"""通知メールの組み立てと送信（Slack を使えない場合の代替ルート）。

エージェントが Slack へ通知するのと同じタイミングで、Application Integration に登録した
メール送信用の連携を実行して通知メールを送ります。

- 宛先は連携の定義に固定されています。このモジュールが渡すのは件名と本文だけなので、
  エージェントが誤動作しても決められた宛先以外へはメールを送れません。
- 着信ポートは不要です。Cloud Run instance から外向きの HTTPS で API を呼び出します。
"""

from __future__ import annotations

import re
import shlex
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from .config import AgentSettings
from .models import AlertEvent, RemediationOutcome, TriageDecision

# メール送信用の連携との取り決め。target-app/scripts/setup_email_notification.py も
# この定数を読み込んで連携を登録するため、送信側と登録側で値がずれない
EMAIL_TRIGGER_ID = "api_trigger/sre_notify_email"
EMAIL_SUBJECT_PARAM = "subject"
EMAIL_BODY_PARAM = "body"

APPROVE_COMMAND_TEMPLATE = "python3 ./target-app/scripts/approve_incident.py {incident_id}"

_MAX_FIELD_CHARS = 2000
_SLACK_MENTION = re.compile(r"<@([A-Za-z0-9]+)>")
# Slack の表示用に付けている装飾（コード表記のバッククォートと太字の **）
_SLACK_EMPHASIS = re.compile(r"`|\*\*")


@dataclass(frozen=True)
class NotificationEmail:
    """通知メール 1 通分の件名と本文（プレーンテキスト）。"""

    subject: str
    body: str


class EmailSendError(RuntimeError):
    """メール送信用の連携の実行に失敗したことを表す例外。"""


class EmailSender(Protocol):
    """本番（Application Integration）とテスト用の送信処理を差し替えるための境界。"""

    def send(self, email: NotificationEmail) -> None:
        """メールを 1 通送信する。失敗時は例外を送出する。"""
        ...


class ApplicationIntegrationEmailSender:
    """Application Integration の連携を実行してメールを送る送信処理。

    成功として扱うのは API が実行を受け付けた場合です。受け付け後にメールが届かなかった場合は
    このクラスからは検知できません。
    """

    def __init__(
        self,
        settings: AgentSettings,
        *,
        session: Any | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        location = settings.email_integration_location
        self._project_id = settings.project_id
        self._url = (
            f"https://{location}-integrations.googleapis.com/v1/projects/{settings.project_id}"
            f"/locations/{location}/integrations/{settings.email_integration_name}:execute"
        )
        self._session = session
        self._timeout_seconds = timeout_seconds
        self._lock = threading.Lock()

    def _get_session(self) -> Any:
        if self._session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
                quota_project_id=self._project_id,
            )
            self._session = AuthorizedSession(credentials)
        return self._session

    def send(self, email: NotificationEmail) -> None:
        payload = {
            "triggerId": EMAIL_TRIGGER_ID,
            "inputParameters": {
                EMAIL_SUBJECT_PARAM: {"stringValue": email.subject},
                EMAIL_BODY_PARAM: {"stringValue": email.body},
            },
        }
        with self._lock:
            response = self._get_session().post(self._url, json=payload, timeout=self._timeout_seconds)
        if response.status_code >= 300:
            raise EmailSendError(
                f"メール送信用の連携の実行に失敗しました (HTTP {response.status_code}): {response.text[:300]}"
            )
        if response.json().get("executionFailed"):
            raise EmailSendError("メール送信用の連携は実行されましたが、失敗として終了しました。")


def _plain(text: str) -> str:
    """Slack 専用の記法（メンションと装飾）を取り除き、長すぎる本文を切り詰める。"""
    cleaned = _SLACK_MENTION.sub(r"\1", text or "")
    cleaned = _SLACK_EMPHASIS.sub("", cleaned).strip()
    if len(cleaned) > _MAX_FIELD_CHARS:
        return cleaned[:_MAX_FIELD_CHARS] + " ...（以下省略）"
    return cleaned


# 最終検証日が無い事例（この環境でまだ一度も検証していない事例）に対して、知識ベースの検索が返す経過日数
NEVER_VERIFIED_AGE_DAYS = 9999.0
NEVER_VERIFIED_LABEL = "この環境では未検証"


def _matched_runbook_line(decision: TriageDecision) -> str:
    runbook_id = decision.matched_runbook_id.strip()
    if not runbook_id or runbook_id == "NONE":
        # 初見の障害では、基準を満たす過去事例がなく ID が空になる
        if decision.similarity_score > 0.0:
            return f"該当なし / 最も近い事例との類似度 {decision.similarity_score:.2f}"
        return "該当なし"
    line = f"{runbook_id} / 類似度 {decision.similarity_score:.2f}"
    if decision.matched_runbook_age_days >= NEVER_VERIFIED_AGE_DAYS:
        line += f" / {NEVER_VERIFIED_LABEL}"
    elif decision.matched_runbook_age_days >= 0.0:
        line += f" / 最終検証 {decision.matched_runbook_age_days:.0f} 日前"
    return line


def _header_lines(alert: AlertEvent, decision: TriageDecision, verdict: str) -> list[str]:
    return [
        f"インシデント ID: {alert.incident_id}",
        f"エラーコード: {alert.error_code}",
        f"対象: {decision.target_namespace}/{decision.target_deployment}",
        f"判定: {verdict}",
        f"操作種別: {decision.remediation_type}",
        f"類似した過去事例: {_matched_runbook_line(decision)}",
    ]


def _section(title: str, text: str) -> list[str]:
    return ["", f"【{title}】", _plain(text)] if text and text.strip() else []


def _outcome_sections(decision: TriageDecision, outcome: RemediationOutcome | None) -> list[str]:
    """処置結果・SLO への効果・Runbook の更新を、Slack の通知カードと同じ内容で並べる。"""
    lines: list[str] = []
    if outcome is not None:
        lines += _section("処置と事後検証の結果", outcome.detail_message)
        if outcome.slo_impact_summary:
            slo_text = outcome.slo_impact_summary
            if outcome.slo_daily_budget_remaining_pct is not None:
                slo_text += f" / 日次 99.0% の残量 {outcome.slo_daily_budget_remaining_pct:.1f}%"
            lines += _section("SLO と Error Budget への効果", slo_text)

        updates: list[str] = []
        if outcome.learned_runbook_id:
            updates.append(
                f"- {outcome.learned_runbook_id} を知識ベースへ新規登録しました。"
                f"次回の推奨区分は {outcome.learned_recommended_action} です。"
            )
        if outcome.refreshed_runbook_id:
            updates.append(f"- {outcome.refreshed_runbook_id} の最終検証日時を現在時刻へ更新しました。")
        if outcome.demoted_runbook_id:
            updates.append(f"- {outcome.demoted_runbook_id} の推奨区分を REQUIRE_APPROVAL へ降格しました。")
        if updates:
            lines += ["", "【Runbook の更新】", *updates]

    lines += _section("回避した誤対応", decision.anti_pattern_warning)
    return lines


def _analysis_sections(alert: AlertEvent, decision: TriageDecision) -> list[str]:
    return [
        *_section("アラートの要約", alert.summary[:300]),
        *_section("根本原因の推定", decision.root_cause_hypothesis),
        *_section("判断根拠", decision.rationale),
    ]


def build_result_email(
    alert: AlertEvent,
    decision: TriageDecision,
    outcome: RemediationOutcome | None,
) -> NotificationEmail:
    """静観または自動復旧の結果を知らせるメールを組み立てる（読むだけで完了する通知）。"""
    if decision.decision == "OBSERVE":
        kind, verdict = "静観", "OBSERVE - 一過性のエラーと判断し、変更操作は行っていません"
    elif outcome is not None and not outcome.verification_passed:
        kind, verdict = "自動復旧後も未回復・要対応", "AUTO_REMEDIATE - 事後検証 NG。オンコール対応が必要です"
    else:
        kind, verdict = "自動復旧完了", "AUTO_REMEDIATE - 自動復旧を実行し、回復を確認しました"

    body = [
        f"■ {kind}",
        "",
        *_header_lines(alert, decision, verdict),
        *_analysis_sections(alert, decision),
        *_outcome_sections(decision, outcome),
        "",
        "この通知への操作は不要です。",
    ]
    return NotificationEmail(
        subject=f"[SRE Agent] {kind}: {alert.error_code} ({alert.incident_id})",
        body="\n".join(body),
    )


def describe_pending_decision(
    alert: AlertEvent,
    decision: TriageDecision,
    *,
    retry_reason: str = "",
) -> list[str]:
    """承認待ちの判定内容をテキストの行にする。

    承認待ちのメールと承認コマンドの画面表示で共用し、人が同じ内容を見て判断できるようにする。
    """
    notes: list[str] = []
    if retry_reason:
        notes += _section(
            "再開に失敗しました",
            f"承認後の再開処理で一時的なエラーが発生したため、承認待ちの状態へ戻しました。"
            f"内容を確認し、もう一度承認または却下を行ってください。エラー: {retry_reason}",
        )
    if decision.stale_revalidation_required:
        notes += _section(
            "Runbook の鮮度再評価",
            "一致した過去の手順は最終検証から長期間が経過しているため、自動実行を止めています。"
            "手順が現在も有効であれば承認すると、実行して最終検証日時を更新します。"
            "陳腐化している場合は却下すると、自動実行の対象から外します。",
        )
    return [
        *_header_lines(alert, decision, "REQUIRE_APPROVAL - 自動変更を止め、人の承認を待っています"),
        *notes,
        *_analysis_sections(alert, decision),
        *_section("回避した誤対応", decision.anti_pattern_warning),
    ]


def build_approval_request_email(
    alert: AlertEvent,
    decision: TriageDecision,
    *,
    retry_reason: str = "",
) -> NotificationEmail:
    """承認待ちを知らせるメールを組み立てる（このメールだけ、受信後にコマンド操作が必要）。

    retry_reason を渡すと、承認後の再開処理が失敗して承認待ちへ戻したことを知らせる文面になる。
    """
    if retry_reason:
        kind = "承認待ち - 再開に失敗"
    elif decision.stale_revalidation_required:
        kind = "承認待ち - 鮮度期限切れの再評価"
    else:
        kind = "承認待ち"

    command = APPROVE_COMMAND_TEMPLATE.format(incident_id=shlex.quote(alert.incident_id))
    body = [
        f"■ {kind}",
        "",
        *describe_pending_decision(alert, decision, retry_reason=retry_reason),
        "",
        "【承認または却下の方法】",
        "リポジトリのルートで次のコマンドを実行してください。",
        "判定内容が表示されるので、確認のうえ approve か reject を入力します。",
        "",
        command,
    ]
    return NotificationEmail(
        subject=f"[SRE Agent] {kind}: {alert.error_code} ({alert.incident_id})",
        body="\n".join(body),
    )


def build_approval_result_email(
    alert: AlertEvent,
    decision: TriageDecision,
    outcome: RemediationOutcome,
    *,
    approved: bool,
    approver: str,
) -> NotificationEmail:
    """承認または却下の結果を知らせるメールを組み立てる（読むだけで完了する通知）。"""
    if not approved:
        kind, verdict = "却下・手動対応へ移行", "REJECTED - 自動処置を中止しました"
    elif not outcome.executed:
        # 手動対応が必要な事象の承認は、変更操作を行わずに引き継ぎを記録するだけ
        kind, verdict = "承認・手動対応へ引き継ぎ", "APPROVED - 変更操作は行わず、手動対応へ引き継ぎました"
    elif not outcome.verification_passed:
        kind, verdict = "承認・実行後も未回復・要対応", "APPROVED - 実行しましたが事後検証 NG です"
    else:
        kind, verdict = "承認・実行完了", "APPROVED - 承認された処置を実行しました"

    body = [
        f"■ {kind}",
        "",
        *_header_lines(alert, decision, verdict),
        f"操作者: {_plain(approver)}",
        *_outcome_sections(decision, outcome),
        "",
        "この通知への操作は不要です。",
    ]
    return NotificationEmail(
        subject=f"[SRE Agent] {kind}: {alert.error_code} ({alert.incident_id})",
        body="\n".join(body),
    )

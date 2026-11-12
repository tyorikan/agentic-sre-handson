"""Slack Socket Mode 双方向通知・Human-in-the-Loop 承認連携モジュール。

Cloud Run instance のインバウンド URL を完全に無効化（`--ingress internal --no-default-url`）したまま、
コンテナから Slack へ外向き TLS WebSocket（Socket Mode）を常時接続して以下を実現します。

1. 静観（OBSERVE）および自動復旧（AUTO_REMEDIATE）の事後レポート通知（Block Kit）
2. 未知障害・高リスク障害時の `[Approve]` / `[Reject]` ボタン送信と、押下者の権限検証
3. 承認ボタン押下をトリガーとした ADK 2.x 中断セッションの再開（Resume）

Slack を使えない環境向けの代替ルートもこのモジュールが担います。

- Slack へ通知するのと同じ箇所から、通知メールも送ります（`email_notifier.py`）。
- 承認・却下は承認コマンドから受け取り、Slack のボタンと同じ再開処理へ渡します（`approval_decisions.py`）。

通知は送信先ごとに独立して送ります。通知の失敗はログへ記録するだけで、判定・復旧・承認の処理は止めません。
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any

from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from .config import AgentSettings
from .email_notifier import (
    ApplicationIntegrationEmailSender,
    EmailSender,
    NotificationEmail,
    build_approval_request_email,
    build_approval_result_email,
    build_result_email,
)
from .models import RemediationOutcome
from .ticket_store import (
    ApprovalTicketStore,
    FirestoreApprovalTicketStore,
    InMemoryApprovalTicketStore,
    StoredApprovalTicket,
)
from .workflow import SreIncidentOrchestrator, WorkflowRunResult

ACTION_APPROVE_ID = "sre_approve_remediation"
ACTION_REJECT_ID = "sre_reject_remediation"
PLACEHOLDER_SLACK_CHANNEL_ID = "C0000000000"


def is_slack_channel_configured(channel_id: str) -> bool:
    """実際の Slack チャンネル ID が設定されているか判定する（テンプレート既定値は未設定扱い）。"""
    stripped = channel_id.strip()
    return bool(stripped and stripped != PLACEHOLDER_SLACK_CHANNEL_ID)


@dataclass
class PendingApprovalTicket:
    """Slack 承認ボタンの応答を待機している中断中ワークフロー情報。"""

    run_result: WorkflowRunResult
    channel_id: str = ""
    message_ts: str = ""


def build_report_blocks(result: WorkflowRunResult) -> list[dict[str, Any]]:
    """静観または自動復旧完了時の Slack Block Kit ペイロードを組み立てる。"""
    dec = result.decision
    out = result.outcome
    if dec.decision == "AUTO_REMEDIATE" and out and not out.verification_passed:
        badge = "🚨 自動復旧未回復・要オンコール対応 (事後検証 NG)"
    elif dec.decision == "REQUIRE_APPROVAL" and dec.stale_revalidation_required:
        badge = "⏳ 承認待機・鮮度期限切れ再評価 (REQUIRE_APPROVAL)"
    else:
        badge = {
            "OBSERVE": "🟢 静観 (OBSERVE - 一過性エラーのため変更なし)",
            "AUTO_REMEDIATE": "🛠️ 自動復旧完了 (AUTO_REMEDIATE)",
            "REQUIRE_APPROVAL": "🟠 承認待機 (REQUIRE_APPROVAL)",
        }[dec.decision]

    age_suffix = (
        f" / 最終検証 `{dec.matched_runbook_age_days:.0f}日前`"
        if dec.matched_runbook_id != "NONE" and dec.matched_runbook_age_days >= 0.0
        else ""
    )

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"SRE Agent Report [{result.alert.incident_id}]"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*判定結果:*\n{badge}"},
                {"type": "mrkdwn", "text": f"*対象:*\n`{dec.target_namespace}/{dec.target_deployment}`"},
                {
                    "type": "mrkdwn",
                    "text": f"*類似過去事例:*\n`{dec.matched_runbook_id}` (類似度 `{dec.similarity_score:.2f}`{age_suffix})",
                },
                {"type": "mrkdwn", "text": f"*操作種別:*\n`{dec.remediation_type}`"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*根本原因の推定:*\n{dec.root_cause_hypothesis}\n\n"
                    f"*判断根拠:*\n{dec.rationale}\n\n"
                    f"*処置・事後検証結果:*\n{out.detail_message if out else 'N/A'}"
                ),
            },
        },
    ]
    if dec.stale_revalidation_required:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            "⏳ *Runbook 鮮度再評価:* 一致した過去手順は最終検証から長期間経過しているため、無断自動実行を一時停止しました。"
                            "手順が現在も有効であれば *Approve & Execute* で実行・鮮度更新、陳腐化していれば *Reject* で自動実行対象から除外します。"
                        ),
                    }
                ],
            }
        )
    if out and out.refreshed_runbook_id:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"🔄 *Runbook 鮮度更新:* `{out.refreshed_runbook_id}` の `last_verified_at` を現在時刻へ更新しました。",
                    }
                ],
            }
        )
    if out and out.demoted_runbook_id:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"🔒 *自動実行凍結:* `{out.demoted_runbook_id}` の推奨区分を `REQUIRE_APPROVAL` へ自動降格しました。",
                    }
                ],
            }
        )
    if not result.paused_for_approval and out and out.slo_impact_summary:
        slo_gauge = (
            f" / 日次99.0%残量 `{out.slo_daily_budget_remaining_pct:.1f}%`"
            if out.slo_daily_budget_remaining_pct is not None
            else ""
        )
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"📊 *SLO & Error Budget 効果:* {out.slo_impact_summary}{slo_gauge}",
                    }
                ],
            }
        )
    if dec.anti_pattern_warning:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"⚠️ *回避した誤対応:* {dec.anti_pattern_warning}"}],
            }
        )
    return blocks


def build_approval_request_blocks(result: WorkflowRunResult) -> list[dict[str, Any]]:
    """人間承認（Approve / Reject ボタン）を要求する Block Kit ペイロードを組み立てる。"""
    blocks = build_report_blocks(result)
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve & Execute"},
                    "style": "primary",
                    "action_id": ACTION_APPROVE_ID,
                    "value": result.alert.incident_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject (Manual Triage)"},
                    "style": "danger",
                    "action_id": ACTION_REJECT_ID,
                    "value": result.alert.incident_id,
                },
            ],
        }
    )
    return blocks


def _log_notification_failure(channel: str, stage: str, incident_id: str, exc: Exception) -> None:
    """通知の失敗を構造化ログへ記録する（判定・復旧・承認の処理は止めない）。"""
    sys.stdout.write(
        json.dumps(
            {
                "severity": "ERROR",
                "event": "notification_failed",
                "channel": channel,
                "stage": stage,
                "incident_id": incident_id,
                "message": f"通知の送信に失敗しました。判定と復旧の処理は続行します: {exc}",
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    sys.stdout.flush()


class SlackHitlCoordinator:
    """通知（Slack・メール）と、人の承認による ADK ワークフロー再開のコーディネーター。"""

    def __init__(
        self,
        settings: AgentSettings,
        orchestrator: SreIncidentOrchestrator,
        *,
        ticket_store: ApprovalTicketStore | None = None,
        email_sender: EmailSender | None = None,
    ) -> None:
        self._settings = settings
        self._orchestrator = orchestrator
        if ticket_store is not None:
            self._ticket_store: ApprovalTicketStore = ticket_store
        elif settings.session_backend.strip().lower() == "firestore":
            self._ticket_store = FirestoreApprovalTicketStore(settings)
        else:
            self._ticket_store = InMemoryApprovalTicketStore()
        self._pending: dict[str, PendingApprovalTicket] = {}
        self._bolt_app: AsyncApp | None = None
        self._socket_handler: AsyncSocketModeHandler | None = None

        if settings.slack_bot_token and settings.slack_app_token:
            self._bolt_app = AsyncApp(token=settings.slack_bot_token)
            self._register_bolt_handlers(self._bolt_app)
            self._socket_handler = AsyncSocketModeHandler(self._bolt_app, settings.slack_app_token)

        self._email_sender: EmailSender | None
        if email_sender is not None:
            self._email_sender = email_sender
        elif settings.email_integration_name:
            self._email_sender = ApplicationIntegrationEmailSender(settings)
        else:
            self._email_sender = None

    @property
    def pending_tickets(self) -> dict[str, PendingApprovalTicket]:
        return dict(self._pending)

    def is_user_authorized(self, slack_user_id: str) -> bool:
        """承認者 ID ホワイトリストが設定されている場合、リスト内のユーザーのみ許可する。"""
        if not self._settings.slack_allowed_approvers:
            return True
        return slack_user_id in self._settings.slack_allowed_approvers

    async def notify_workflow_result(self, result: WorkflowRunResult) -> None:
        """ワークフロー結果を Slack とメールへ通知する（承認待ちの場合は先に永続チケットを保存する）。

        通知は送信先ごとに独立して送る。片方が失敗してももう片方は送り、失敗はログへ記録するだけにする。
        判定と復旧はこの時点で完了しているため、通知の失敗を理由にやり直させない。
        """
        incident_id = result.alert.incident_id

        stored_ticket: StoredApprovalTicket | None = None
        if result.paused_for_approval and result.confirmation_call_id:
            # 承認操作と保存のレースを防ぐため、通知を送る前に永続化しておく
            stored_ticket = StoredApprovalTicket(
                incident_id=incident_id,
                alert=result.alert,
                decision=result.decision,
                session_id=result.session_id,
                invocation_id=result.invocation_id,
                confirmation_call_id=result.confirmation_call_id,
            )
            await self._ticket_store.save_ticket(stored_ticket)
            self._pending[incident_id] = PendingApprovalTicket(run_result=result)

        slack_enabled = bool(self._bolt_app and is_slack_channel_configured(self._settings.slack_channel_id))
        if slack_enabled:
            try:
                await self._post_to_slack(result, stored_ticket)
            except Exception as exc:  # noqa: BLE001
                _log_notification_failure("slack", "post", incident_id, exc)

        if self._email_sender is not None:
            email = (
                build_approval_request_email(result.alert, result.decision)
                if result.paused_for_approval
                else build_result_email(result.alert, result.decision, result.outcome)
            )
            await self._send_email(email, incident_id, "post")

        if not slack_enabled and self._email_sender is None:
            # 通知先が未設定の環境（ローカル検証等）では構造化ログとして出力
            sys.stdout.write(
                json.dumps(
                    {
                        "severity": "INFO",
                        "event": "slack_notification_fallback",
                        "incident_id": incident_id,
                        "paused_for_approval": result.paused_for_approval,
                        "decision": result.decision.model_dump(),
                        "outcome": result.outcome.model_dump() if result.outcome else None,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            sys.stdout.flush()

    async def _post_to_slack(self, result: WorkflowRunResult, stored_ticket: StoredApprovalTicket | None) -> None:
        """Slack へ通知カードを投稿し、承認待ちの場合は投稿先をチケットへ記録する。"""
        assert self._bolt_app is not None
        blocks = (
            build_approval_request_blocks(result)
            if result.paused_for_approval
            else build_report_blocks(result)
        )
        resp = await self._bolt_app.client.chat_postMessage(
            channel=self._settings.slack_channel_id,
            text=f"[{result.alert.incident_id}] SRE Agent Decision: {result.decision.decision}",
            blocks=blocks,
        )
        if stored_ticket is not None:
            channel_id = str(resp["channel"])
            message_ts = str(resp["ts"])
            updated_ticket = stored_ticket.model_copy(
                update={"channel_id": channel_id, "message_ts": message_ts}
            )
            await self._ticket_store.save_ticket(updated_ticket)
            if result.alert.incident_id in self._pending:
                self._pending[result.alert.incident_id].channel_id = channel_id
                self._pending[result.alert.incident_id].message_ts = message_ts

    async def _send_email(self, email: NotificationEmail, incident_id: str, stage: str) -> None:
        """通知メールを送る。未設定なら何もしない。失敗はログへ記録して処理を続ける。"""
        if self._email_sender is None:
            return
        try:
            await asyncio.to_thread(self._email_sender.send, email)
        except Exception as exc:  # noqa: BLE001
            _log_notification_failure("email", stage, incident_id, exc)

    async def _update_slack_card(
        self,
        ticket: StoredApprovalTicket,
        *,
        text: str,
        blocks: list[dict[str, Any]],
        stage: str,
    ) -> None:
        """Slack の通知カードを書き換える。投稿先がなければ何もしない。失敗はログへ記録して処理を続ける。"""
        if not (self._bolt_app and ticket.channel_id and ticket.message_ts):
            return
        try:
            await self._bolt_app.client.chat_update(
                channel=ticket.channel_id,
                ts=ticket.message_ts,
                text=text,
                blocks=blocks,
            )
        except Exception as exc:  # noqa: BLE001
            _log_notification_failure("slack", stage, ticket.incident_id, exc)

    async def process_approval_decision(
        self,
        *,
        incident_id: str,
        slack_user_id: str,
        approved: bool,
    ) -> RemediationOutcome:
        """Slack のボタン押下を検証し、永続チケットと中断 ADK セッションから処理を再開する。"""
        if not self.is_user_authorized(slack_user_id):
            raise PermissionError(
                f"Slack ユーザー '{slack_user_id}' は承認許可リストに含まれていないため操作を拒否しました。"
            )
        return await self._resolve_approval(
            incident_id=incident_id,
            approver_id=slack_user_id,
            approver_label=f"<@{slack_user_id}>",
            approved=approved,
        )

    async def process_command_decision(
        self,
        *,
        incident_id: str,
        approver: str,
        approved: bool,
    ) -> RemediationOutcome:
        """承認コマンドからの承認・却下で、永続チケットと中断 ADK セッションから処理を再開する。

        承認できるのは承認専用トピックへの発行権限を持つ主体。approver は自己申告のため、
        監査記録と通知の表示にだけ使い、権限の判定には使わない。
        """
        return await self._resolve_approval(
            incident_id=incident_id,
            approver_id=approver,
            approver_label=approver,
            approved=approved,
        )

    async def _resolve_approval(
        self,
        *,
        incident_id: str,
        approver_id: str,
        approver_label: str,
        approved: bool,
    ) -> RemediationOutcome:
        """承認・却下の共通処理。チケットは 1 回だけ取得でき、二重の承認は KeyError で拒否する。"""
        ticket = await self._ticket_store.claim_ticket(incident_id)
        if ticket is None or not ticket.confirmation_call_id:
            raise KeyError(f"incident_id='{incident_id}' の承認待ちセッションが存在しないか、既に処理済みです。")

        self._pending.pop(incident_id, None)

        # 承認の受付直後にボタンを消去して「承認受付・実行中...」へ更新し、二度押しと待機不安を防ぐ。
        # 書き換えに失敗しても、承認された処理は続行する。
        if approved:
            progress_label = "⏳ 承認受付・実行中..."
            if ticket.decision.remediation_type in ("ROLLBACK_DEPLOYMENT", "RESTART_DEPLOYMENT"):
                progress_detail = (
                    f"`{ticket.decision.remediation_type}` の実行と事後エラー率プローブ検証を行っています"
                    "（完了まで約 30〜45 秒かかります。完了次第このカードが自動更新されます）..."
                )
            else:
                progress_detail = "オンコール引き継ぎの監査記録と Runbook 鮮度更新を実行しています..."
        else:
            progress_label = "⏳ 却下受付・手動対応へ切替中..."
            progress_detail = "自動処置を中止し、監査ログおよび知識ベースの更新を実行しています..."

        await self._update_slack_card(
            ticket,
            text=f"[{incident_id}] {progress_label} by {approver_label}",
            blocks=[
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": f"SRE Agent Report [{incident_id}]"},
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*{progress_label}* (操作者: {approver_label})\n"
                            f"*対象:* `{ticket.decision.target_namespace}/{ticket.decision.target_deployment}`  |  "
                            f"*操作種別:* `{ticket.decision.remediation_type}`\n"
                            f"*進行状況:* {progress_detail}"
                        ),
                    },
                },
            ],
            stage="progress",
        )

        try:
            outcome = await self._orchestrator.resume_from_approval(
                alert=ticket.alert,
                decision=ticket.decision,
                session_id=ticket.session_id,
                invocation_id=ticket.invocation_id,
                confirmation_call_id=ticket.confirmation_call_id,
                approved=approved,
                approver_id=approver_id,
                approver_label=approver_label,
            )
        except Exception as exc:
            # 自動では再試行しない。承認待ちへ戻し、人がもう一度判断できるように知らせる
            await self._ticket_store.release_ticket(incident_id)
            restore_blocks = build_approval_request_blocks(
                WorkflowRunResult(
                    alert=ticket.alert,
                    session_id=ticket.session_id,
                    invocation_id=ticket.invocation_id,
                    decision=ticket.decision,
                    paused_for_approval=True,
                    confirmation_call_id=ticket.confirmation_call_id,
                    outcome=RemediationOutcome(
                        executed=False,
                        action_status="AWAITING_APPROVAL",
                        detail_message=f"⚠️ 再開処理中に一時エラーが発生したため承認待ち状態へ戻しました: {exc}",
                    ),
                )
            )
            await self._update_slack_card(
                ticket,
                text=f"[{incident_id}] 承認待ち状態へ復元しました",
                blocks=restore_blocks,
                stage="restore",
            )
            await self._send_email(
                build_approval_request_email(ticket.alert, ticket.decision, retry_reason=str(exc)),
                incident_id,
                "restore",
            )
            raise

        await self._ticket_store.delete_ticket(incident_id)

        action_label = "✅ 承認・実行完了" if approved else "🛑 却下・手動対応へ移行"
        kb_note = (
            f"\n📚 *自律ナレッジ登録:* `{outcome.learned_runbook_id}` を BigQuery `incident_runbooks` へ追加しました"
            f"（AI.EMBED 自動ベクトル化・次回推奨: `{outcome.learned_recommended_action}`）"
            if outcome.learned_runbook_id
            else ""
        )
        refresh_note = (
            f"\n🔄 *Runbook 鮮度更新:* `{outcome.refreshed_runbook_id}` の `last_verified_at` を現在時刻へ更新しました"
            f"（今後 {self._settings.runbook_freshness_ttl_days} 日間有効）"
            if outcome.refreshed_runbook_id
            else ""
        )
        demote_note = (
            f"\n🔒 *自動実行凍結:* `{outcome.demoted_runbook_id}` の推奨区分を `REQUIRE_APPROVAL` へ降格しました"
            "（今後の無断自動実行を抑止）"
            if outcome.demoted_runbook_id
            else ""
        )
        slo_gauge_suffix = (
            f" / 日次99.0%残量 `{outcome.slo_daily_budget_remaining_pct:.1f}%`"
            if outcome.slo_daily_budget_remaining_pct is not None
            else ""
        )
        slo_note = (
            f"\n📊 *SLO & Error Budget 効果:* {outcome.slo_impact_summary}{slo_gauge_suffix}"
            if outcome.slo_impact_summary
            else ""
        )
        # 処置と記録は完了しているため、結果の通知に失敗しても成功として返す
        await self._update_slack_card(
            ticket,
            text=f"[{incident_id}] {action_label} by {approver_label}",
            blocks=[
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": f"SRE Agent Report [{incident_id}]"},
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*{action_label}* (操作者: {approver_label})\n"
                            f"*対象:* `{ticket.decision.target_namespace}/{ticket.decision.target_deployment}`\n"
                            f"*結果:* {outcome.detail_message}"
                            f"{slo_note}{kb_note}{refresh_note}{demote_note}"
                        ),
                    },
                },
            ],
            stage="result",
        )
        await self._send_email(
            build_approval_result_email(
                ticket.alert,
                ticket.decision,
                outcome,
                approved=approved,
                approver=approver_id,
            ),
            incident_id,
            "result",
        )
        return outcome

    def _register_bolt_handlers(self, app: AsyncApp) -> None:
        """Slack Bolt の Interactive Action ハンドラを登録する。"""

        @app.action(ACTION_APPROVE_ID)
        async def _on_approve(ack: Any, body: dict[str, Any], respond: Any) -> None:
            await ack()
            user_id = body.get("user", {}).get("id", "unknown")
            incident_id = body.get("actions", [{}])[0].get("value", "")
            try:
                await self.process_approval_decision(
                    incident_id=incident_id,
                    slack_user_id=user_id,
                    approved=True,
                )
            except Exception as exc:  # noqa: BLE001
                await respond(text=f"⚠️ 承認処理エラー: {exc}", replace_original=False)

        @app.action(ACTION_REJECT_ID)
        async def _on_reject(ack: Any, body: dict[str, Any], respond: Any) -> None:
            await ack()
            user_id = body.get("user", {}).get("id", "unknown")
            incident_id = body.get("actions", [{}])[0].get("value", "")
            try:
                await self.process_approval_decision(
                    incident_id=incident_id,
                    slack_user_id=user_id,
                    approved=False,
                )
            except Exception as exc:  # noqa: BLE001
                await respond(text=f"⚠️ 却下処理エラー: {exc}", replace_original=False)

    async def start_socket_mode(self) -> None:
        """Slack Socket Mode (Outbound WebSocket) 接続を開始する。"""
        if self._socket_handler is not None:
            await self._socket_handler.connect_async()

    async def close_socket_mode(self) -> None:
        """Slack Socket Mode 接続を安全にクローズする。"""
        if self._socket_handler is not None:
            await self._socket_handler.close_async()

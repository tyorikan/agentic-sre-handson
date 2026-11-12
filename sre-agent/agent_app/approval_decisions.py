"""承認コマンドからの承認・却下イベントの検証と受け渡し（Slack を使えない場合の代替ルート）。

承認コマンド（target-app/scripts/approve_incident.py）は、承認専用の Pub/Sub トピックへ
承認または却下のイベントを発行します。常駐エージェントは専用サブスクリプションから
StreamingPull（外向き接続）で受け取り、Slack のボタンと同じ再開処理へ渡します。

安全のための決まり:
- アラート用トピックとは別のトピックを使う。アラート用には監視対象アプリのログが流れるため、
  同じトピックで承認を受け付けると、ログを 1 行出すだけで承認を偽装できてしまう。
- 承認できるのは、承認専用トピックへの発行権限を持つ主体。Pub/Sub は発行者を証明しないので、
  approver は自己申告の監査記録としてだけ扱う。
- 発行から時間が経ったイベントは実行しない。エージェントの停止中に溜まった承認が、
  状況が変わった後で実行されるのを防ぐ。
- 失敗しても自動で再試行しない。承認待ちへ戻し、人がもう一度判断する。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from .slack_socket import SlackHitlCoordinator

# 発行からこの秒数を超えた承認・却下は実行しない
MAX_DECISION_AGE_SECONDS = 300.0

# 自己申告の操作者名に使える文字。通知や監査記録へそのまま載るため、英数字と一部の記号に限定する
APPROVER_PATTERN = r"^[A-Za-z0-9@._+\-]{1,200}$"

HandlingStatus = Literal["applied", "invalid", "expired", "not_pending", "failed"]


class ApprovalDecisionMessage(BaseModel):
    """承認専用トピックに流れる承認・却下イベント。形式が少しでも違うものは受け付けない。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["approval_decision"]
    incident_id: str = Field(min_length=1, max_length=200)
    approved: bool
    approver: str = Field(pattern=APPROVER_PATTERN)


@dataclass(frozen=True)
class ApprovalHandlingResult:
    """承認・却下イベント 1 件の処理結果（ログ出力とテストの確認用）。"""

    status: HandlingStatus
    incident_id: str = ""
    detail: str = ""


async def handle_approval_decision_message(
    raw: bytes,
    coordinator: "SlackHitlCoordinator",
    *,
    published_at: datetime | None = None,
    now: datetime | None = None,
    max_age_seconds: float = MAX_DECISION_AGE_SECONDS,
) -> ApprovalHandlingResult:
    """承認・却下イベントを検証し、有効なら中断中のワークフローを再開する。

    どの結果でも例外は送出しない。呼び出し側はメッセージを ack し、自動の再配信に頼らない。
    """
    try:
        message = ApprovalDecisionMessage.model_validate_json(raw)
    except ValidationError as exc:
        return ApprovalHandlingResult(status="invalid", detail=f"形式が正しくないため無視しました: {exc.error_count()} 件の検証エラー")

    if published_at is not None:
        current = now or datetime.now(timezone.utc)
        age_seconds = (current - published_at).total_seconds()
        if age_seconds > max_age_seconds:
            return ApprovalHandlingResult(
                status="expired",
                incident_id=message.incident_id,
                detail=f"発行から {age_seconds:.0f} 秒が経過しているため実行しませんでした。承認待ちのまま残しています。",
            )

    try:
        outcome = await coordinator.process_command_decision(
            incident_id=message.incident_id,
            approver=message.approver,
            approved=message.approved,
        )
    except KeyError as exc:
        return ApprovalHandlingResult(status="not_pending", incident_id=message.incident_id, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - 失敗の種類を問わず自動再試行しないため、ここで受け止める
        return ApprovalHandlingResult(status="failed", incident_id=message.incident_id, detail=str(exc))

    return ApprovalHandlingResult(status="applied", incident_id=message.incident_id, detail=outcome.action_status)

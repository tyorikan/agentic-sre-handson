#!/usr/bin/env python3
"""承認待ちのインシデントを、ターミナルから承認または却下するコマンド（Slack を使えない場合の代替ルート）。

エージェントから「承認待ち」のメールが届いたら、メールに書かれたコマンドをそのまま実行します。

動き:
  1. Firestore から承認待ちチケットを 1 回だけ読み取り、判定内容を表示する。
  2. approve か reject の入力を受け取る。それ以外の入力では何も発行せずに終了する。
  3. 承認専用の Pub/Sub トピックへ承認または却下を発行して終了する。
  4. 結果はエージェントからのメールで届く。このコマンドは完了を待たない。

使い方:
  # 承認待ちの一覧を表示（メールが届かなかった場合の確認用）
  python3 ./target-app/scripts/approve_incident.py

  # 判定内容を確認し、approve か reject を入力
  python3 ./target-app/scripts/approve_incident.py <incident_id>

必要な権限:
  - Firestore の読み取り（roles/datastore.viewer）
  - 承認専用トピックへの発行（roles/pubsub.publisher）。この発行権限を持つことが、承認できる条件です。

注意:
  このコマンドは人が内容を確認して実行します。AI コーディングエージェントや自動化スクリプトから実行しないでください。
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from google.api_core import exceptions as gcp_exceptions
from google.auth import exceptions as auth_exceptions
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "sre-agent"))

from agent_app.approval_decisions import APPROVER_PATTERN, ApprovalDecisionMessage  # noqa: E402
from agent_app.email_notifier import APPROVE_COMMAND_TEMPLATE, describe_pending_decision  # noqa: E402
from agent_app.ticket_store import StoredApprovalTicket, ticket_document_id  # noqa: E402

DEFAULT_APPROVAL_TOPIC = "sre-approval-decisions"
DEFAULT_TICKETS_COLLECTION = "sre_pending_approval_tickets"
LIST_COMMAND = APPROVE_COMMAND_TEMPLATE.format(incident_id="").strip()
_RULE = "=" * 72


def load_ticket(client: Any, collection: str, incident_id: str) -> StoredApprovalTicket | None:
    """承認待ちチケットを Firestore から 1 回だけ読み取る（待ち受けや繰り返しの読み取りはしない）。"""
    snap = client.collection(collection).document(ticket_document_id(incident_id)).get()
    if not snap.exists:
        return None
    return StoredApprovalTicket.model_validate(snap.to_dict() or {})


def list_tickets(client: Any, collection: str) -> list[StoredApprovalTicket]:
    """承認待ちチケットの一覧を 1 回のクエリで読み取り、古い順に並べる。"""
    tickets: list[StoredApprovalTicket] = []
    for snap in client.collection(collection).stream():
        try:
            tickets.append(StoredApprovalTicket.model_validate(snap.to_dict() or {}))
        except ValidationError:
            continue
    return sorted(tickets, key=lambda ticket: ticket.created_at)


def _elapsed_label(created_at: str, now: datetime) -> str:
    """チケットの作成からの経過時間を、人が読みやすい表記にする。"""
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return "不明"
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    minutes = max(0, int((now - created).total_seconds() // 60))
    if minutes < 1:
        return "1 分未満"
    if minutes < 120:
        return f"{minutes} 分"
    return f"{minutes // 60} 時間"


def describe_choices(ticket: StoredApprovalTicket) -> list[str]:
    """approve と reject のそれぞれで何が起きるかを、入力の直前に示す。"""
    decision = ticket.decision
    target = f"{decision.target_namespace}/{decision.target_deployment}"
    if decision.remediation_type in ("ROLLBACK_DEPLOYMENT", "RESTART_DEPLOYMENT"):
        approve_effect = f"{target} に {decision.remediation_type} を実行し、実行後のエラー率まで確認します。"
    else:
        approve_effect = "変更操作は行いません。手動対応へ引き継いだことを記録します。"
    return [
        "【入力すると起きること】",
        f"  approve : {approve_effect}",
        "  reject  : 自動処置を中止し、手動対応へ切り替えたことを記録します。",
    ]


def format_ticket_details(ticket: StoredApprovalTicket, now: datetime) -> str:
    """承認待ちチケット 1 件の判定内容を、承認待ちのメールと同じ並びで表示用に整える。"""
    lines = [
        _RULE,
        f"承認待ちのインシデント（承認待ちになってから {_elapsed_label(ticket.created_at, now)}）",
        _RULE,
        *describe_pending_decision(ticket.alert, ticket.decision),
        "",
        *describe_choices(ticket),
        _RULE,
    ]
    return "\n".join(lines)


def format_ticket_list(tickets: list[StoredApprovalTicket], now: datetime) -> str:
    """承認待ちチケットの一覧を表示用に整える。"""
    if not tickets:
        return "承認待ちのインシデントはありません。"
    lines = [f"承認待ちのインシデント: {len(tickets)} 件", ""]
    for ticket in tickets:
        decision = ticket.decision
        state = "承認待ち" if ticket.status == "PENDING" else "承認または却下を処理中"
        lines += [
            f"- {ticket.incident_id}",
            f"    {ticket.alert.error_code} / {decision.remediation_type} / "
            f"{decision.target_namespace}/{decision.target_deployment}",
            f"    状態: {state} / 経過: {_elapsed_label(ticket.created_at, now)}",
            f"    確認して承認または却下: {APPROVE_COMMAND_TEMPLATE.format(incident_id=shlex.quote(ticket.incident_id))}",
            "",
        ]
    return "\n".join(lines).rstrip()


def parse_decision_input(raw: str) -> bool | None:
    """入力を承認（True）・却下（False）・中止（None）に分ける。approve と reject 以外はすべて中止。"""
    answer = raw.strip().lower()
    if answer == "approve":
        return True
    if answer == "reject":
        return False
    return None


def _gcloud_account() -> str:
    """gcloud にログイン中のアカウントを返す（取得できなければ空文字）。"""
    try:
        done = subprocess.run(
            ["gcloud", "config", "get-value", "account"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip()


def resolve_approver(
    explicit: str,
    *,
    gcloud_account: Callable[[], str] = _gcloud_account,
    os_user: Callable[[], str] = getpass.getuser,
) -> str:
    """監査記録へ残す操作者名を決める（--approver、gcloud のアカウント、OS のユーザー名の順）。

    操作者名は自己申告で、承認の権限には関係しない。使える文字はエージェント側の検証に合わせる。
    """
    if explicit:
        if not re.fullmatch(APPROVER_PATTERN, explicit):
            raise ValueError("--approver に使える文字は、英数字と @ . _ + - だけです（200 文字以内）。")
        return explicit
    for candidate in (gcloud_account(), os_user()):
        if candidate and re.fullmatch(APPROVER_PATTERN, candidate):
            return candidate
    raise ValueError("操作者名を自動で決められませんでした。--approver で指定してください。")


def build_decision_message(incident_id: str, approved: bool, approver: str) -> ApprovalDecisionMessage:
    """承認専用トピックへ発行するイベントを、エージェント側と同じ検証を通して組み立てる。"""
    return ApprovalDecisionMessage(
        kind="approval_decision",
        incident_id=incident_id,
        approved=approved,
        approver=approver,
    )


def publish_decision(publisher: Any, topic_path: str, message: ApprovalDecisionMessage) -> str:
    """承認または却下を承認専用トピックへ発行し、メッセージ ID を返す。"""
    future = publisher.publish(topic_path, data=message.model_dump_json().encode("utf-8"))
    return str(future.result(timeout=30))


def _build_firestore_client(project: str, database: str) -> Any:
    from google.cloud import firestore

    return firestore.Client(project=project, database=database)


def _build_publisher() -> Any:
    from google.cloud import pubsub_v1

    return pubsub_v1.PublisherClient()


def run(
    argv: list[str] | None = None,
    *,
    firestore_client: Any | None = None,
    publisher: Any | None = None,
    input_fn: Callable[[str], str] = input,
    now: datetime | None = None,
) -> int:
    """コマンド本体。終了コードは、発行または一覧表示に成功したら 0、それ以外は 1。"""
    load_dotenv(REPO_ROOT / ".env")

    parser = argparse.ArgumentParser(description="承認待ちのインシデントを承認または却下する")
    parser.add_argument("incident_id", nargs="?", default="", help="承認待ちのメールに書かれたインシデント ID。省略すると一覧を表示")
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT", ""), help="Google Cloud プロジェクト ID")
    parser.add_argument("--topic", default=os.getenv("APPROVAL_PUBSUB_TOPIC", DEFAULT_APPROVAL_TOPIC), help="承認専用の Pub/Sub トピック名")
    parser.add_argument("--database", default=os.getenv("FIRESTORE_DATABASE", "(default)"), help="Firestore のデータベース名")
    parser.add_argument(
        "--collection",
        default=os.getenv("FIRESTORE_TICKETS_COLLECTION", DEFAULT_TICKETS_COLLECTION),
        help="承認待ちチケットのコレクション名",
    )
    parser.add_argument("--approver", default="", help="監査記録へ残す操作者名。省略時は gcloud のアカウント")
    args = parser.parse_args(argv)

    if not args.project or args.project == "your-sandbox-project-id":
        print("エラー: GOOGLE_CLOUD_PROJECT 環境変数または --project を設定してください。", file=sys.stderr)
        return 1

    current = now or datetime.now(timezone.utc)

    try:
        client = firestore_client or _build_firestore_client(args.project, args.database)
        if not args.incident_id:
            print(format_ticket_list(list_tickets(client, args.collection), current))
            return 0
        ticket = load_ticket(client, args.collection, args.incident_id)
    except auth_exceptions.GoogleAuthError as exc:
        print(
            f"エラー: Google Cloud の認証情報を取得できませんでした。"
            f"gcloud auth application-default login を実行してから、もう一度お試しください: {exc}",
            file=sys.stderr,
        )
        return 1
    except gcp_exceptions.GoogleAPICallError as exc:
        print(
            f"エラー: Firestore から承認待ちチケットを読み取れませんでした。読み取り権限 (roles/datastore.viewer) を確認してください: {exc}",
            file=sys.stderr,
        )
        return 1

    if ticket is None:
        print(
            f"インシデント '{args.incident_id}' は承認待ちではありません。処理済みか、ID が違います。\n"
            f"承認待ちの一覧は次のコマンドで確認できます:\n  {LIST_COMMAND}",
            file=sys.stderr,
        )
        return 1
    if ticket.status != "PENDING":
        print(
            f"インシデント '{ticket.incident_id}' は承認または却下を処理中です。結果のメールを待ってください。",
            file=sys.stderr,
        )
        return 1

    try:
        approver = resolve_approver(args.approver)
    except ValueError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    print(format_ticket_details(ticket, current))
    print(f"操作者として記録する名前: {approver}")
    try:
        answer = input_fn("approve か reject を入力してください（それ以外は中止）: ")
    except EOFError:
        answer = ""
    approved = parse_decision_input(answer)
    if approved is None:
        print("中止しました。承認も却下も発行していません。")
        return 1

    message = build_decision_message(ticket.incident_id, approved, approver)
    topic_path = f"projects/{args.project}/topics/{args.topic}"
    try:
        message_id = publish_decision(publisher or _build_publisher(), topic_path, message)
    except gcp_exceptions.NotFound:
        print(
            f"エラー: 承認専用トピック '{args.topic}' が見つかりません。セットアップ手順で作成してください。何も発行していません。",
            file=sys.stderr,
        )
        return 1
    except gcp_exceptions.PermissionDenied:
        print(
            f"エラー: 承認専用トピック '{args.topic}' への発行権限 (roles/pubsub.publisher) がありません。"
            "承認できるのは、この権限を持つ人だけです。何も発行していません。",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - 発行の失敗理由を問わず、発行できたか分からない旨を伝える
        print(f"エラー: 発行に失敗しました。一覧を表示して状態を確認してください: {exc}", file=sys.stderr)
        return 1

    label = "承認" if approved else "却下"
    print(
        f"\n{label}を発行しました（メッセージ ID: {message_id}）。\n"
        "エージェントが処理を再開します。結果はメールで届きます。このコマンドは完了を待たずに終了します。\n"
        "再開に失敗した場合は、エラーの内容を添えた承認待ちのメールがもう一度届きます。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())

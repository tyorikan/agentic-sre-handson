"""Slack を使えない場合の代替ルート（メール通知 + 承認コマンド）と、通知が失敗した時の扱いのテスト。

確認する内容:
  - 承認コマンドからの承認・却下が、Slack のボタンと同じ再開処理へ届くこと
  - アラート用トピックに承認の形をしたメッセージを流しても承認されないこと
  - 4 種類の通知メールの件名と本文
  - Slack やメールの送信に失敗しても、判定・復旧・承認の処理が止まらず、やり直されないこと
  - 承認コマンドとセットアップスクリプトの動き
"""

from __future__ import annotations

import importlib.util
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from google.adk.sessions import DatabaseSessionService
from pydantic import ValidationError

from agent_app.approval_decisions import ApprovalDecisionMessage, handle_approval_decision_message
from agent_app.config import AgentSettings
from agent_app.dedup_store import IncidentGuardrailManager, InMemoryEventStoreBackend
from agent_app.email_notifier import (
    EMAIL_BODY_PARAM,
    EMAIL_SUBJECT_PARAM,
    EMAIL_TRIGGER_ID,
    ApplicationIntegrationEmailSender,
    EmailSendError,
    NotificationEmail,
    build_approval_request_email,
    build_approval_result_email,
    build_result_email,
)
from agent_app.main import parse_pubsub_payload, process_incoming_alert
from agent_app.models import AlertEvent, RemediationOutcome, SandboxMetricsAnalysis, SimilarRunbookMatch, TriageDecision
from agent_app.remediation_tools import SafeRemediationService
from agent_app.sandbox_runner import SandboxRunner
from agent_app.slack_socket import SlackHitlCoordinator
from agent_app.ticket_store import FirestoreApprovalTicketStore
from agent_app.vector_kb import LocalJsonlKnowledgeBase, synthesize_runbook_deterministic
from agent_app.workflow import SreIncidentOrchestrator

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "target-app" / "scripts"
NOW = datetime(2026, 11, 12, 0, 0, tzinfo=timezone.utc)

AUTO_REMEDIATE_ALERT = {
    "error_code": "CONFIG_KEY_ERROR_500",
    "summary": "KeyError: PAYMENT_CURRENCY_PRECISION missing after deployment rollout",
    "error_rate_series": [0.01, 0.85, 0.90, 0.92],
    "recent_deployment_within_30m": True,
}
OBSERVE_ALERT = {
    "error_code": "UPSTREAM_CONN_RESET_503",
    "summary": "Upstream payment gateway connection reset by peer during TLS handshake",
    "error_rate_series": [0.45, 0.50, 0.18, 0.03, 0.01],
    "recent_deployment_within_30m": False,
}
APPROVAL_ALERT = {
    "error_code": "DB_CONN_STALL_500",
    "summary": "DB connection stall",
    "error_rate_series": [0.4, 0.5],
}


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


approve_incident = _load_script("approve_incident")
setup_email_notification = _load_script("setup_email_notification")


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """開発者の .env やシェルの環境変数が、スクリプトの既定値へ混ざらないようにする。"""
    for module in (approve_incident, setup_email_notification):
        monkeypatch.setattr(module, "load_dotenv", lambda *_args, **_kwargs: False)
    for name in (
        "APPROVAL_PUBSUB_TOPIC",
        "APPROVAL_PUBSUB_SUBSCRIPTION",
        "EMAIL_NOTIFY_INTEGRATION",
        "EMAIL_NOTIFY_LOCATION",
        "NOTIFY_EMAIL_TO",
        "FIRESTORE_DATABASE",
        "FIRESTORE_TICKETS_COLLECTION",
        "GOOGLE_CLOUD_REGION",
    ):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------------------
# テスト用の差し替え部品
# --------------------------------------------------------------------------------------


class CountingK8s:
    """GKE への操作を記録するだけの差し替え部品。"""

    def __init__(self) -> None:
        self.rollbacks: list[tuple[str, str]] = []
        self.restarts: list[tuple[str, str]] = []

    def rollback_deployment(self, namespace: str, deployment_name: str) -> str:
        self.rollbacks.append((namespace, deployment_name))
        return f"Rolled back {namespace}/{deployment_name}"

    def restart_deployment(self, namespace: str, deployment_name: str) -> str:
        self.restarts.append((namespace, deployment_name))
        return f"Restarted {namespace}/{deployment_name}"


class RecordingEmailSender:
    """送信されたメールを記録する差し替え部品。fail=True なら毎回失敗する。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.attempts = 0
        self.sent: list[NotificationEmail] = []

    def send(self, email: NotificationEmail) -> None:
        self.attempts += 1
        if self.fail:
            raise EmailSendError("simulated email failure")
        self.sent.append(email)


class FlakySlackClient:
    """指定した呼び出しだけ失敗する Slack クライアントの差し替え部品。"""

    def __init__(self, *, fail_post: bool = False, fail_update_calls: tuple[int, ...] = ()) -> None:
        self.fail_post = fail_post
        self.fail_update_calls = fail_update_calls
        self.post_calls = 0
        self.update_calls = 0
        self.posted: list[dict[str, Any]] = []
        self.update_texts: list[str] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:  # noqa: N802 - Slack SDK のメソッド名
        self.post_calls += 1
        if self.fail_post:
            raise RuntimeError("simulated Slack failure (chat.postMessage)")
        self.posted.append(kwargs)
        return {"channel": "C_TEST", "ts": "1700000000.000100"}

    async def chat_update(self, **kwargs: Any) -> dict[str, bool]:
        self.update_calls += 1
        if self.update_calls in self.fail_update_calls:
            raise RuntimeError(f"simulated Slack failure (chat.update #{self.update_calls})")
        self.update_texts.append(str(kwargs.get("text", "")))
        return {"ok": True}


class _Snap:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return dict(self._data) if self._data is not None else None


class SharedFirestore:
    """エージェント（非同期）と承認コマンド（同期）が同じチケットを読むための、メモリ上の Firestore。"""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, dict[str, Any]]] = {}
        self.command_reads = 0

    def for_agent(self) -> Any:
        shared = self

        class _Doc:
            def __init__(self, store: dict[str, dict[str, Any]], doc_id: str) -> None:
                self._store, self._doc_id = store, doc_id

            async def set(self, data: dict[str, Any]) -> None:
                self._store[self._doc_id] = dict(data)

            async def get(self) -> _Snap:
                return _Snap(self._store.get(self._doc_id))

            async def update(self, fields: dict[str, Any]) -> None:
                self._store[self._doc_id].update(fields)

            async def delete(self) -> None:
                self._store.pop(self._doc_id, None)

        class _Client:
            def collection(self, name: str) -> Any:
                store = shared.collections.setdefault(name, {})
                return SimpleNamespace(document=lambda doc_id: _Doc(store, doc_id))

        return _Client()

    def for_command(self) -> Any:
        shared = self

        class _Doc:
            def __init__(self, store: dict[str, dict[str, Any]], doc_id: str) -> None:
                self._store, self._doc_id = store, doc_id

            def get(self) -> _Snap:
                shared.command_reads += 1
                return _Snap(self._store.get(self._doc_id))

        class _Collection:
            def __init__(self, store: dict[str, dict[str, Any]]) -> None:
                self._store = store

            def document(self, doc_id: str) -> _Doc:
                return _Doc(self._store, doc_id)

            def stream(self) -> list[_Snap]:
                shared.command_reads += 1
                return [_Snap(data) for data in self._store.values()]

        class _Client:
            def collection(self, name: str) -> _Collection:
                return _Collection(shared.collections.setdefault(name, {}))

        return _Client()


class FakePublisher:
    """承認専用トピックへの発行を記録する差し替え部品。"""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    def publish(self, topic_path: str, data: bytes) -> Any:
        self.published.append((topic_path, data))
        return SimpleNamespace(result=lambda timeout=None: f"msg-{len(self.published)}")


async def _triage_for_test(
    alert: AlertEvent,
    sandbox_stats: SandboxMetricsAnalysis,
    matches: list[SimilarRunbookMatch],
) -> TriageDecision:
    """アラートの内容に応じて、静観・自動復旧・承認待ちを決め打ちで返す推論の差し替え部品。"""
    top = matches[0]
    if alert.error_code == AUTO_REMEDIATE_ALERT["error_code"]:
        return TriageDecision(
            decision=top.recommended_action,
            remediation_type=top.remediation_type,
            target_namespace=alert.namespace,
            target_deployment=alert.service_name,
            matched_runbook_id=top.runbook_id,
            similarity_score=top.similarity_score,
            root_cause_hypothesis=top.root_cause,
            rationale=f"過去事例 {top.runbook_id} と高い類似度で一致。",
        )
    if alert.error_code == OBSERVE_ALERT["error_code"] and sandbox_stats.is_naturally_decaying:
        return TriageDecision(
            decision="OBSERVE",
            remediation_type="NONE",
            target_namespace=alert.namespace,
            target_deployment=alert.service_name,
            matched_runbook_id=top.runbook_id,
            similarity_score=top.similarity_score,
            root_cause_hypothesis=top.root_cause,
            rationale="エラー率が自然に減衰しているため静観を選択。",
        )
    return TriageDecision(
        decision="REQUIRE_APPROVAL",
        remediation_type="RESTART_DEPLOYMENT",
        target_namespace=alert.namespace,
        target_deployment=alert.service_name,
        matched_runbook_id="INC-039",
        similarity_score=0.81,
        root_cause_hypothesis="接続プールが枯渇しており、再起動で初期化できる見込み。",
        rationale="過去事例との類似度が基準に届かないため、人の承認を求めます。",
    )


async def _synthesize_for_test(alert: AlertEvent, decision: TriageDecision, outcome: RemediationOutcome) -> Any:
    return synthesize_runbook_deterministic(alert, decision, outcome)


def _build_env(
    tmp_path: Path,
    *,
    slack: FlakySlackClient | None = None,
    email: RecordingEmailSender | None = None,
    firestore: SharedFirestore | None = None,
) -> SimpleNamespace:
    """フェイクの GKE・知識ベース・通知先で、エージェントの処理一式を組み立てる。"""
    settings = AgentSettings(
        project_id="test-proj",
        sandbox_mode="local_subprocess",
        session_backend="sqlite",
        session_db_url=f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}",
        slack_bot_token="",
        slack_app_token="",
        slack_channel_id="C_TEST" if slack is not None else "",
        slack_allowed_approvers=("U_SRE_LEAD",),
        email_integration_name="",
        approval_subscription="",
    )
    store = InMemoryEventStoreBackend()
    guardrails = IncidentGuardrailManager(settings, store)
    k8s = CountingK8s()
    orchestrator = SreIncidentOrchestrator(
        settings=settings,
        sandbox_runner=SandboxRunner(settings),
        knowledge_base=LocalJsonlKnowledgeBase(REPO_ROOT / "bigquery" / "seed_runbooks_100.jsonl"),
        guardrail_manager=guardrails,
        remediation_service=SafeRemediationService(settings, k8s, post_error_rate_probe=lambda _ns, _svc: 0.0),
        decision_analyzer=_triage_for_test,
        runbook_synthesizer=_synthesize_for_test,
        session_service=DatabaseSessionService(db_url=settings.session_db_url),
    )
    ticket_store = (
        FirestoreApprovalTicketStore(settings, client=firestore.for_agent()) if firestore is not None else None
    )
    coordinator = SlackHitlCoordinator(settings, orchestrator, ticket_store=ticket_store, email_sender=email)
    if slack is not None:
        coordinator._bolt_app = SimpleNamespace(client=slack)  # type: ignore[assignment]  # noqa: SLF001
    return SimpleNamespace(
        settings=settings,
        store=store,
        guardrails=guardrails,
        k8s=k8s,
        orchestrator=orchestrator,
        coordinator=coordinator,
    )


async def _deliver_alert(env: SimpleNamespace, alert: AlertEvent) -> bool:
    """アラート用トピックからの受信 1 回分を処理する（main.py の受信処理と同じ関数を呼ぶ）。"""
    return await process_incoming_alert(alert, env.guardrails, env.orchestrator, env.coordinator)


def _decision_event(incident_id: str, *, approved: bool, approver: str = "operator@example.com") -> bytes:
    return (
        ApprovalDecisionMessage(kind="approval_decision", incident_id=incident_id, approved=approved, approver=approver)
        .model_dump_json()
        .encode("utf-8")
    )


def _decision(**overrides: Any) -> TriageDecision:
    base: dict[str, Any] = {
        "decision": "REQUIRE_APPROVAL",
        "remediation_type": "RESTART_DEPLOYMENT",
        "target_namespace": "sre-demo-app",
        "target_deployment": "order-service",
        "matched_runbook_id": "INC-039",
        "similarity_score": 0.62,
        "root_cause_hypothesis": "メモリ上の参照表が破損していると推定されます。",
        "rationale": "初見の持続エラーのため、人の承認後に再起動を提案します。",
    }
    base.update(overrides)
    return TriageDecision(**base)


# --------------------------------------------------------------------------------------
# 承認コマンドからの承認・却下イベント
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_command_approval_resumes_workflow_and_emails_result(tmp_path: Path) -> None:
    """承認イベントで中断中の処理が再開し、承認待ちと承認結果のメールが 1 通ずつ届くこと。"""
    email = RecordingEmailSender()
    env = _build_env(tmp_path, email=email)
    alert = AlertEvent(incident_id="CMD-APPROVE-1", **APPROVAL_ALERT)

    assert await _deliver_alert(env, alert) is True
    assert env.k8s.restarts == []
    request_mail = email.sent[0]
    assert request_mail.subject == "[SRE Agent] 承認待ち: DB_CONN_STALL_500 (CMD-APPROVE-1)"
    assert "python3 ./target-app/scripts/approve_incident.py CMD-APPROVE-1" in request_mail.body

    # Slack の承認者リスト (U_SRE_LEAD) に関係なく、承認専用トピックへ発行できた承認は受け付ける
    handled = await handle_approval_decision_message(
        _decision_event("CMD-APPROVE-1", approved=True), env.coordinator, published_at=NOW, now=NOW
    )

    assert handled.status == "applied"
    assert env.k8s.restarts == [("sre-demo-app", "order-service")]
    assert env.coordinator.pending_tickets == {}
    assert any(r.incident_id == "CMD-APPROVE-1" and r.decision == "APPROVED_EXECUTED" for r in env.store.rows)

    assert len(email.sent) == 2
    result_mail = email.sent[1]
    assert result_mail.subject == "[SRE Agent] 承認・実行完了: DB_CONN_STALL_500 (CMD-APPROVE-1)"
    assert "操作者: operator@example.com" in result_mail.body
    assert "を知識ベースへ新規登録しました。" in result_mail.body
    assert "この通知への操作は不要です。" in result_mail.body


@pytest.mark.asyncio
async def test_command_rejection_stops_remediation_and_emails_result(tmp_path: Path) -> None:
    """却下イベントでは GKE を操作せず、却下の記録とメールが残ること。"""
    email = RecordingEmailSender()
    env = _build_env(tmp_path, email=email)
    await _deliver_alert(env, AlertEvent(incident_id="CMD-REJECT-1", **APPROVAL_ALERT))

    handled = await handle_approval_decision_message(
        _decision_event("CMD-REJECT-1", approved=False), env.coordinator, published_at=NOW, now=NOW
    )

    assert handled.status == "applied"
    assert env.k8s.restarts == []
    assert any(r.incident_id == "CMD-REJECT-1" and r.decision == "REJECTED" for r in env.store.rows)
    result_mail = email.sent[-1]
    assert result_mail.subject == "[SRE Agent] 却下・手動対応へ移行: DB_CONN_STALL_500 (CMD-REJECT-1)"
    assert "承認者 operator@example.com が自動操作を却下し" in result_mail.body


@pytest.mark.asyncio
async def test_unknown_ticket_and_double_approval_are_not_applied(tmp_path: Path) -> None:
    """存在しないチケットと 2 回目の承認は実行されないこと（承認は 1 チケット 1 回限り）。"""
    env = _build_env(tmp_path, email=RecordingEmailSender())
    await _deliver_alert(env, AlertEvent(incident_id="CMD-ONCE-1", **APPROVAL_ALERT))

    unknown = await handle_approval_decision_message(_decision_event("NO-SUCH-ID", approved=True), env.coordinator)
    first = await handle_approval_decision_message(_decision_event("CMD-ONCE-1", approved=True), env.coordinator)
    second = await handle_approval_decision_message(_decision_event("CMD-ONCE-1", approved=True), env.coordinator)

    assert unknown.status == "not_pending"
    assert first.status == "applied"
    assert second.status == "not_pending"
    assert env.k8s.restarts == [("sre-demo-app", "order-service")]


@pytest.mark.asyncio
async def test_malformed_approval_events_are_ignored(tmp_path: Path) -> None:
    """形式が少しでも違う承認イベントは無視され、チケットは承認待ちのまま残ること。"""
    env = _build_env(tmp_path, email=RecordingEmailSender())
    await _deliver_alert(env, AlertEvent(incident_id="CMD-MALFORMED-1", **APPROVAL_ALERT))
    valid = json.loads(_decision_event("CMD-MALFORMED-1", approved=True))

    malformed_events = [
        b"not json",
        b"{}",
        json.dumps({**valid, "approved": "true"}).encode(),  # 真偽値ではなく文字列
        json.dumps({**valid, "kind": "alert"}).encode(),
        json.dumps({**valid, "force": True}).encode(),  # 定義にない項目
        json.dumps({**valid, "incident_id": ""}).encode(),
        json.dumps({**valid, "approver": "<@U123456>"}).encode(),  # 通知先で解釈される記法
        json.dumps({**valid, "approver": "operator\nadmin"}).encode(),
        json.dumps({k: v for k, v in valid.items() if k != "approver"}).encode(),
    ]
    for raw in malformed_events:
        handled = await handle_approval_decision_message(raw, env.coordinator, published_at=NOW, now=NOW)
        assert handled.status == "invalid", raw

    assert "CMD-MALFORMED-1" in env.coordinator.pending_tickets
    assert env.k8s.restarts == []


@pytest.mark.asyncio
async def test_expired_approval_event_is_not_executed(tmp_path: Path) -> None:
    """発行から 300 秒を超えた承認は実行せず、チケットを承認待ちのまま残すこと。"""
    env = _build_env(tmp_path, email=RecordingEmailSender())
    await _deliver_alert(env, AlertEvent(incident_id="CMD-EXPIRED-1", **APPROVAL_ALERT))
    raw = _decision_event("CMD-EXPIRED-1", approved=True)

    expired = await handle_approval_decision_message(
        raw, env.coordinator, published_at=NOW - timedelta(seconds=301), now=NOW
    )
    assert expired.status == "expired"
    assert env.k8s.restarts == []
    assert "CMD-EXPIRED-1" in env.coordinator.pending_tickets

    fresh = await handle_approval_decision_message(
        raw, env.coordinator, published_at=NOW - timedelta(seconds=299), now=NOW
    )
    assert fresh.status == "applied"
    assert env.k8s.restarts == [("sre-demo-app", "order-service")]


@pytest.mark.asyncio
async def test_approval_shaped_message_on_alert_topic_does_not_approve(tmp_path: Path) -> None:
    """監視対象アプリのログが流れるアラート用トピックからは、承認を偽装できないこと。"""
    env = _build_env(tmp_path, email=RecordingEmailSender())
    await _deliver_alert(env, AlertEvent(incident_id="SPOOF-TARGET-1", **APPROVAL_ALERT))
    approval_fields = json.loads(_decision_event("SPOOF-TARGET-1", approved=True, approver="attacker"))

    # 1. 承認イベントをそのままアラート用トピックへ流しても、アラートとして解釈できず破棄される
    with pytest.raises(ValidationError):
        parse_pubsub_payload(json.dumps(approval_fields).encode())

    # 2. 監視対象アプリが承認の形をしたログを出しても、別の新しいアラートとして扱われるだけ
    spoof_log = {
        "insertId": "spoof-log-entry-1",
        "severity": "ERROR",
        "resource": {"labels": {"namespace_name": "sre-demo-app", "container_name": "order-service"}},
        "jsonPayload": {**approval_fields, "message": "forged approval"},
    }
    spoofed_alert = parse_pubsub_payload(json.dumps(spoof_log).encode())
    assert spoofed_alert.incident_id == "spoof-log-entry-1"
    await _deliver_alert(env, spoofed_alert)

    assert env.k8s.restarts == [] and env.k8s.rollbacks == []
    assert "SPOOF-TARGET-1" in env.coordinator.pending_tickets


@pytest.mark.asyncio
async def test_resume_failure_returns_ticket_to_pending_and_resends_approval_email(tmp_path: Path) -> None:
    """再開に失敗したら自動で再試行せず、承認待ちへ戻して、理由を添えた承認待ちメールを送り直すこと。"""
    email = RecordingEmailSender()
    env = _build_env(tmp_path, email=email)
    await _deliver_alert(env, AlertEvent(incident_id="CMD-RETRY-1", **APPROVAL_ALERT))

    original_resume = env.orchestrator.resume_from_approval
    calls = {"count": 0}

    async def _fail_once(**kwargs: Any) -> RemediationOutcome:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("simulated session store outage")
        return await original_resume(**kwargs)

    env.orchestrator.resume_from_approval = _fail_once  # type: ignore[method-assign]

    failed = await handle_approval_decision_message(_decision_event("CMD-RETRY-1", approved=True), env.coordinator)

    assert failed.status == "failed"
    assert calls["count"] == 1  # 自動では再試行しない
    assert env.k8s.restarts == []
    retry_mail = email.sent[-1]
    assert retry_mail.subject == "[SRE Agent] 承認待ち - 再開に失敗: DB_CONN_STALL_500 (CMD-RETRY-1)"
    assert "simulated session store outage" in retry_mail.body
    assert "python3 ./target-app/scripts/approve_incident.py CMD-RETRY-1" in retry_mail.body

    # 人がもう一度承認すれば、同じチケットから再開できる
    retried = await handle_approval_decision_message(_decision_event("CMD-RETRY-1", approved=True), env.coordinator)
    assert retried.status == "applied"
    assert env.k8s.restarts == [("sre-demo-app", "order-service")]


# --------------------------------------------------------------------------------------
# 通知メールの内容
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observe_and_auto_remediation_results_are_emailed(tmp_path: Path) -> None:
    """静観と自動復旧の結果が、Slack へ通知するのと同じ箇所からメールでも届くこと。"""
    email = RecordingEmailSender()
    env = _build_env(tmp_path, email=email)

    assert await _deliver_alert(env, AlertEvent(incident_id="MAIL-OBSERVE-1", **OBSERVE_ALERT)) is True
    assert await _deliver_alert(env, AlertEvent(incident_id="MAIL-AUTO-1", **AUTO_REMEDIATE_ALERT)) is True

    observe_mail, auto_mail = email.sent
    assert observe_mail.subject == "[SRE Agent] 静観: UPSTREAM_CONN_RESET_503 (MAIL-OBSERVE-1)"
    assert "変更操作は行っていません" in observe_mail.body
    assert auto_mail.subject == "[SRE Agent] 自動復旧完了: CONFIG_KEY_ERROR_500 (MAIL-AUTO-1)"
    assert "操作種別: ROLLBACK_DEPLOYMENT" in auto_mail.body
    assert "【処置と事後検証の結果】" in auto_mail.body
    for mail in (observe_mail, auto_mail):
        assert "この通知への操作は不要です。" in mail.body
        assert "approve_incident.py" not in mail.body
    assert env.k8s.rollbacks == [("sre-demo-app", "order-service")]


def test_result_email_reports_slo_effect_runbook_updates_and_failed_verification() -> None:
    """結果メールに SLO への効果と Runbook の更新が載り、事後検証 NG は完了と表示しないこと。"""
    alert = AlertEvent(incident_id="MAIL-RESULT-1", error_code="CONFIG_KEY_ERROR_500", summary="KeyError after rollout")
    decision = _decision(decision="AUTO_REMEDIATE", remediation_type="ROLLBACK_DEPLOYMENT", matched_runbook_id="INC-001")
    passed = RemediationOutcome(
        executed=True,
        action_status="ROLLED_BACK",
        detail_message="ロールバック後のエラー率は 0.00% です。",
        verification_passed=True,
        refreshed_runbook_id="INC-001",
        slo_impact_summary="実測 MTTR `82秒` — Error Budget 消費を **`28.6分` 削減**",
        slo_daily_budget_remaining_pct=91.2,
    )
    failed = RemediationOutcome(
        executed=True,
        action_status="ROLLED_BACK",
        detail_message="操作後もエラー率が 25.00% 残っています。",
        verification_passed=False,
        demoted_runbook_id="INC-001",
    )

    passed_mail = build_result_email(alert, decision, passed)
    failed_mail = build_result_email(alert, decision, failed)

    assert passed_mail.subject == "[SRE Agent] 自動復旧完了: CONFIG_KEY_ERROR_500 (MAIL-RESULT-1)"
    # Slack の表示用の装飾（バッククォートと **）は、メールでは取り除く
    assert "実測 MTTR 82秒 — Error Budget 消費を 28.6分 削減 / 日次 99.0% の残量 91.2%" in passed_mail.body
    assert "- INC-001 の最終検証日時を現在時刻へ更新しました。" in passed_mail.body
    assert failed_mail.subject == "[SRE Agent] 自動復旧後も未回復・要対応: CONFIG_KEY_ERROR_500 (MAIL-RESULT-1)"
    assert "自動復旧完了" not in failed_mail.body
    assert "- INC-001 の推奨区分を REQUIRE_APPROVAL へ降格しました。" in failed_mail.body


def test_approval_request_email_variants_and_plain_text_rules() -> None:
    """承認待ちメールの 3 つの文面と、Slack 専用の記法や長すぎる本文の扱いを確認する。"""
    alert = AlertEvent(incident_id="MAIL-REQ-1", error_code="TAX_RULE_CRC32_MISMATCH_500", summary="checksum mismatch")

    normal = build_approval_request_email(alert, _decision())
    stale = build_approval_request_email(alert, _decision(stale_revalidation_required=True, matched_runbook_age_days=120.0))
    retry = build_approval_request_email(alert, _decision(), retry_reason="deadline exceeded")

    assert normal.subject == "[SRE Agent] 承認待ち: TAX_RULE_CRC32_MISMATCH_500 (MAIL-REQ-1)"
    assert "【根本原因の推定】" in normal.body and "【判断根拠】" in normal.body
    assert "類似した過去事例: INC-039 / 類似度 0.62" in normal.body
    assert "approve か reject を入力します。" in normal.body
    assert stale.subject == "[SRE Agent] 承認待ち - 鮮度期限切れの再評価: TAX_RULE_CRC32_MISMATCH_500 (MAIL-REQ-1)"
    assert "【Runbook の鮮度再評価】" in stale.body and "最終検証 120 日前" in stale.body
    assert retry.subject == "[SRE Agent] 承認待ち - 再開に失敗: TAX_RULE_CRC32_MISMATCH_500 (MAIL-REQ-1)"
    assert "【再開に失敗しました】" in retry.body and "deadline exceeded" in retry.body

    # 初見の障害で一致する過去事例がない場合は、空欄ではなく「該当なし」と表示する
    novel = build_approval_request_email(alert, _decision(matched_runbook_id="", similarity_score=0.81))
    assert "類似した過去事例: 該当なし / 最も近い事例との類似度 0.81" in novel.body
    unmatched = build_approval_request_email(alert, _decision(matched_runbook_id="NONE", similarity_score=0.0))
    assert "類似した過去事例: 該当なし\n" in unmatched.body

    # シェルで特別な意味を持つ文字を含む ID は、そのまま貼り付けても安全なように引用符で囲む
    odd = build_approval_request_email(alert.model_copy(update={"incident_id": "a b;c"}), _decision())
    assert "approve_incident.py 'a b;c'" in odd.body

    # Slack 専用のメンション記法は取り除き、長すぎる項目は切り詰める
    outcome = RemediationOutcome(
        executed=False,
        action_status="REJECTED",
        detail_message="承認者 <@U123ABC> が自動操作を却下しました。" + "あ" * 3000,
    )
    result = build_approval_result_email(alert, _decision(), outcome, approved=False, approver="U123ABC")
    assert "<@" not in result.body
    assert "承認者 U123ABC が自動操作を却下しました。" in result.body
    assert "...（以下省略）" in result.body
    assert len(result.body) < 2600


def test_approval_result_email_distinguishes_each_outcome() -> None:
    """承認結果メールが、実行完了・実行後も未回復・却下・手動対応への引き継ぎを区別して伝えること。"""
    alert = AlertEvent(incident_id="MAIL-RES-1", error_code="DB_CONN_STALL_500", summary="stall")
    decision = _decision()
    executed_ok = RemediationOutcome(
        executed=True,
        action_status="RESTARTED",
        detail_message="再起動後のエラー率は 0.00% です。",
        verification_passed=True,
        learned_runbook_id="AUTO-1A2B3C4D",
        learned_recommended_action="AUTO_REMEDIATE",
    )
    executed_ng = RemediationOutcome(
        executed=True, action_status="RESTARTED", detail_message="エラーが残っています。", verification_passed=False
    )
    rejected = RemediationOutcome(executed=False, action_status="REJECTED", detail_message="却下しました。")
    handoff = RemediationOutcome(
        executed=False,
        action_status="MANUAL_HANDOFF_ACKNOWLEDGED",
        detail_message="オンコール担当者が手動調査・対処を引き継ぎ承認しました。",
        verification_passed=True,
    )

    ok_mail = build_approval_result_email(alert, decision, executed_ok, approved=True, approver="operator@example.com")
    ng_mail = build_approval_result_email(alert, decision, executed_ng, approved=True, approver="operator@example.com")
    rejected_mail = build_approval_result_email(alert, decision, rejected, approved=False, approver="operator@example.com")
    handoff_mail = build_approval_result_email(alert, decision, handoff, approved=True, approver="operator@example.com")

    assert ok_mail.subject == "[SRE Agent] 承認・実行完了: DB_CONN_STALL_500 (MAIL-RES-1)"
    assert "- AUTO-1A2B3C4D を知識ベースへ新規登録しました。次回の推奨区分は AUTO_REMEDIATE です。" in ok_mail.body
    assert ng_mail.subject == "[SRE Agent] 承認・実行後も未回復・要対応: DB_CONN_STALL_500 (MAIL-RES-1)"
    assert rejected_mail.subject == "[SRE Agent] 却下・手動対応へ移行: DB_CONN_STALL_500 (MAIL-RES-1)"
    # 変更操作を伴わない承認は「実行完了」と書かない
    assert handoff_mail.subject == "[SRE Agent] 承認・手動対応へ引き継ぎ: DB_CONN_STALL_500 (MAIL-RES-1)"
    assert "変更操作は行わず、手動対応へ引き継ぎました" in handoff_mail.body
    assert "実行完了" not in handoff_mail.body


# --------------------------------------------------------------------------------------
# 通知が失敗した時の扱い
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slack_fails", "email_fails"),
    [(True, False), (False, True), (True, True)],
    ids=["slack_only_fails", "email_only_fails", "both_fail"],
)
async def test_notification_failure_does_not_abort_or_repeat_remediation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    slack_fails: bool,
    email_fails: bool,
) -> None:
    """通知が失敗しても例外にせず、もう片方へは送り、再配信されても復旧をやり直さないこと。"""
    slack = FlakySlackClient(fail_post=slack_fails)
    email = RecordingEmailSender(fail=email_fails)
    env = _build_env(tmp_path, slack=slack, email=email)
    alert = AlertEvent(incident_id="NOTIFY-FAIL-1", **AUTO_REMEDIATE_ALERT)

    assert await _deliver_alert(env, alert) is True  # 例外にならないので、受信側は再配信を要求しない
    assert env.k8s.rollbacks == [("sre-demo-app", "order-service")]
    assert slack.post_calls == 1 and email.attempts == 1  # 送信先ごとに独立して 1 回ずつ試みる
    assert len(slack.posted) == (0 if slack_fails else 1)
    assert len(email.sent) == (0 if email_fails else 1)

    assert await _deliver_alert(env, alert) is False  # 同じアラートが再配信されても重複として扱う
    assert env.k8s.rollbacks == [("sre-demo-app", "order-service")]
    assert slack.post_calls == 1 and email.attempts == 1  # 通知の再送もしない

    logged = capsys.readouterr().out
    assert ('"event": "notification_failed", "channel": "slack"' in logged) is slack_fails
    assert ('"event": "notification_failed", "channel": "email"' in logged) is email_fails


@pytest.mark.asyncio
async def test_pending_ticket_can_be_approved_even_if_every_notification_failed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """承認待ちの通知がすべて失敗しても、チケットは保存済みで、承認コマンドの一覧から承認できること。"""
    shared = SharedFirestore()
    env = _build_env(
        tmp_path,
        slack=FlakySlackClient(fail_post=True),
        email=RecordingEmailSender(fail=True),
        firestore=shared,
    )

    assert await _deliver_alert(env, AlertEvent(incident_id="NOTIFY-LOST-1", **APPROVAL_ALERT)) is True
    capsys.readouterr()

    exit_code = approve_incident.run(["--project", "test-proj"], firestore_client=shared.for_command(), now=NOW)
    listed = capsys.readouterr().out
    assert exit_code == 0
    assert "承認待ちのインシデント: 1 件" in listed
    assert "python3 ./target-app/scripts/approve_incident.py NOTIFY-LOST-1" in listed

    handled = await handle_approval_decision_message(_decision_event("NOTIFY-LOST-1", approved=True), env.coordinator)
    assert handled.status == "applied"
    assert env.k8s.restarts == [("sre-demo-app", "order-service")]


@pytest.mark.asyncio
async def test_slack_card_update_failures_do_not_block_approval(tmp_path: Path) -> None:
    """Slack のカードの書き換えに失敗しても、承認された復旧を実行し、正常に終えること。"""
    # 「実行中」への書き換え（1 回目）が失敗する場合
    progress_slack = FlakySlackClient(fail_update_calls=(1,))
    (tmp_path / "progress").mkdir()
    env = _build_env(tmp_path / "progress", slack=progress_slack, email=RecordingEmailSender())
    await _deliver_alert(env, AlertEvent(incident_id="CARD-PROGRESS-1", **APPROVAL_ALERT))

    outcome = await env.coordinator.process_approval_decision(
        incident_id="CARD-PROGRESS-1", slack_user_id="U_SRE_LEAD", approved=True
    )

    assert outcome.executed is True
    assert env.k8s.restarts == [("sre-demo-app", "order-service")]
    assert progress_slack.update_texts == ["[CARD-PROGRESS-1] ✅ 承認・実行完了 by <@U_SRE_LEAD>"]
    with pytest.raises(KeyError):  # チケットは処理中のまま残らず、消費済みになっている
        await env.coordinator.process_approval_decision(
            incident_id="CARD-PROGRESS-1", slack_user_id="U_SRE_LEAD", approved=True
        )

    # 最終結果への書き換え（2 回目）が失敗する場合
    final_slack = FlakySlackClient(fail_update_calls=(2,))
    final_email = RecordingEmailSender()
    (tmp_path / "final").mkdir()
    env2 = _build_env(tmp_path / "final", slack=final_slack, email=final_email)
    await _deliver_alert(env2, AlertEvent(incident_id="CARD-FINAL-1", **APPROVAL_ALERT))

    outcome2 = await env2.coordinator.process_approval_decision(
        incident_id="CARD-FINAL-1", slack_user_id="U_SRE_LEAD", approved=True
    )

    assert outcome2.executed is True and outcome2.action_status == "RESTARTED"
    assert env2.k8s.restarts == [("sre-demo-app", "order-service")]
    assert final_email.sent[-1].subject == "[SRE Agent] 承認・実行完了: DB_CONN_STALL_500 (CARD-FINAL-1)"


@pytest.mark.asyncio
async def test_slack_and_email_are_both_notified_when_both_are_configured(tmp_path: Path) -> None:
    """Slack とメールの両方を設定した場合、Slack の表示は従来どおりで、メールにも同じ節目で届くこと。"""
    slack = FlakySlackClient()
    email = RecordingEmailSender()
    env = _build_env(tmp_path, slack=slack, email=email)

    await _deliver_alert(env, AlertEvent(incident_id="BOTH-1", **APPROVAL_ALERT))
    posted = slack.posted[0]
    assert posted["channel"] == "C_TEST"
    assert posted["text"] == "[BOTH-1] SRE Agent Decision: REQUIRE_APPROVAL"
    assert posted["blocks"][-1]["type"] == "actions"  # Approve / Reject ボタン

    with pytest.raises(PermissionError):  # Slack のボタンは従来どおり承認者リストで制限する
        await env.coordinator.process_approval_decision(incident_id="BOTH-1", slack_user_id="U_OTHER", approved=True)
    await env.coordinator.process_approval_decision(incident_id="BOTH-1", slack_user_id="U_SRE_LEAD", approved=True)

    assert slack.update_texts == [
        "[BOTH-1] ⏳ 承認受付・実行中... by <@U_SRE_LEAD>",
        "[BOTH-1] ✅ 承認・実行完了 by <@U_SRE_LEAD>",
    ]
    assert [mail.subject for mail in email.sent] == [
        "[SRE Agent] 承認待ち: DB_CONN_STALL_500 (BOTH-1)",
        "[SRE Agent] 承認・実行完了: DB_CONN_STALL_500 (BOTH-1)",
    ]
    assert "操作者: U_SRE_LEAD" in email.sent[1].body


# --------------------------------------------------------------------------------------
# 承認コマンド
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_command_shows_decision_and_publishes_event_accepted_by_agent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """承認コマンドが判定内容を表示し、発行したイベントをエージェントがそのまま受け付けること。"""
    shared = SharedFirestore()
    env = _build_env(tmp_path, email=RecordingEmailSender(), firestore=shared)
    await _deliver_alert(env, AlertEvent(incident_id="CMD-E2E-1", **APPROVAL_ALERT))
    capsys.readouterr()
    publisher = FakePublisher()

    exit_code = approve_incident.run(
        ["CMD-E2E-1", "--project", "test-proj", "--approver", "operator@example.com"],
        firestore_client=shared.for_command(),
        publisher=publisher,
        input_fn=lambda _prompt: " Approve ",
        now=NOW,
    )

    shown = capsys.readouterr().out
    assert exit_code == 0
    for expected in (
        "インシデント ID: CMD-E2E-1",
        "エラーコード: DB_CONN_STALL_500",
        "対象: sre-demo-app/order-service",
        "操作種別: RESTART_DEPLOYMENT",
        "【根本原因の推定】",
        "【判断根拠】",
        "approve : sre-demo-app/order-service に RESTART_DEPLOYMENT を実行し",
        "reject  : 自動処置を中止し",
        "操作者として記録する名前: operator@example.com",
        "承認を発行しました",
    ):
        assert expected in shown, expected
    assert shared.command_reads == 1  # チケットの読み取りは 1 回だけ（待ち受けや繰り返しの読み取りはしない）

    topic_path, payload = publisher.published[0]
    assert topic_path == "projects/test-proj/topics/sre-approval-decisions"
    handled = await handle_approval_decision_message(payload, env.coordinator, published_at=NOW, now=NOW)
    assert handled.status == "applied"
    assert env.k8s.restarts == [("sre-demo-app", "order-service")]


@pytest.mark.asyncio
async def test_approve_command_publishes_nothing_unless_approve_or_reject_is_entered(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """approve と reject 以外の入力、存在しない ID、処理中のチケットでは何も発行しないこと。"""
    shared = SharedFirestore()
    env = _build_env(tmp_path, email=RecordingEmailSender(), firestore=shared)
    await _deliver_alert(env, AlertEvent(incident_id="CMD-ABORT-1", **APPROVAL_ALERT))
    publisher = FakePublisher()
    common = {"firestore_client": shared.for_command(), "publisher": publisher, "now": NOW}
    args = ["CMD-ABORT-1", "--project", "test-proj", "--approver", "operator@example.com"]

    def _end_of_input(_prompt: str) -> str:
        raise EOFError

    for answer_fn in (lambda _p: "yes", lambda _p: "y", lambda _p: "", lambda _p: "approve!", _end_of_input):
        assert approve_incident.run(args, input_fn=answer_fn, **common) == 1
    assert "中止しました。承認も却下も発行していません。" in capsys.readouterr().out

    assert approve_incident.run(["NO-SUCH-ID", "--project", "test-proj"], input_fn=lambda _p: "approve", **common) == 1
    assert "承認待ちではありません" in capsys.readouterr().err

    tickets = shared.collections[env.settings.firestore_tickets_collection]
    tickets["CMD-ABORT-1"]["status"] = "PROCESSING"
    assert approve_incident.run(args, input_fn=lambda _p: "approve", **common) == 1
    assert "処理中です" in capsys.readouterr().err

    assert publisher.published == []

    # reject と入力した場合は却下のイベントを発行する
    tickets["CMD-ABORT-1"]["status"] = "PENDING"
    assert approve_incident.run(args, input_fn=lambda _p: "reject", **common) == 0
    assert json.loads(publisher.published[0][1]) == {
        "kind": "approval_decision",
        "incident_id": "CMD-ABORT-1",
        "approved": False,
        "approver": "operator@example.com",
    }


def test_approve_command_operator_name_and_manual_handoff_wording() -> None:
    """操作者名の決め方と、変更操作を伴わない承認の説明文を確認する。"""
    resolve = approve_incident.resolve_approver
    assert resolve("explicit@example.com", gcloud_account=lambda: "g@example.com", os_user=lambda: "u") == "explicit@example.com"
    assert resolve("", gcloud_account=lambda: "g@example.com", os_user=lambda: "u") == "g@example.com"
    assert resolve("", gcloud_account=lambda: "", os_user=lambda: "local-user") == "local-user"
    with pytest.raises(ValueError, match="--approver に使える文字"):
        resolve("山田 太郎", gcloud_account=lambda: "", os_user=lambda: "u")
    with pytest.raises(ValueError, match="--approver で指定"):
        resolve("", gcloud_account=lambda: "", os_user=lambda: "名前 に 空白")

    assert approve_incident.parse_decision_input("REJECT\n") is False
    assert approve_incident.parse_decision_input("approve it") is None

    ticket = approve_incident.StoredApprovalTicket(
        incident_id="HANDOFF-1",
        alert=AlertEvent(incident_id="HANDOFF-1", summary="manual"),
        decision=_decision(remediation_type="MANUAL_INTERVENTION"),
        session_id="s",
        invocation_id="i",
        confirmation_call_id="c",
        created_at=(NOW - timedelta(minutes=7)).isoformat(),
    )
    details = approve_incident.format_ticket_details(ticket, NOW)
    assert "approve : 変更操作は行いません。手動対応へ引き継いだことを記録します。" in details
    assert "承認待ちのインシデント（承認待ちになってから 7 分）" in details


# --------------------------------------------------------------------------------------
# メール送信用の連携（セットアップと送信）
# --------------------------------------------------------------------------------------


class _HttpResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload, ensure_ascii=False)
        self.content = self.text.encode("utf-8")

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeIntegrationService:
    """Application Integration と割り当ての REST API を、メモリ上で最小限に再現する差し替え部品。"""

    PARENT = "projects/test-proj/locations/asia-northeast1"
    # 実際の API と同じく、応答のリソース名ではプロジェクト番号を返す
    STORED_PARENT = "projects/123456789/locations/asia-northeast1"

    def __init__(self, *, provisioned: bool = False, daily_limit: int | None = 5000) -> None:
        self.provisioned = provisioned
        self.daily_limit = daily_limit
        self.versions: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, str, dict[str, str] | None]] = []
        self.executions: list[tuple[str, dict[str, Any]]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,  # noqa: A002 - requests の引数名に合わせる
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> _HttpResponse:
        if "serviceusage.googleapis.com" in url:
            if self.daily_limit is None:
                return _HttpResponse(403, {"error": {"status": "PERMISSION_DENIED"}})
            return _HttpResponse(
                200,
                {
                    "metrics": [
                        {"metric": "integrations.googleapis.com/other_metric"},
                        {
                            "metric": "integrations.googleapis.com/email_sent_count",
                            "consumerQuotaLimits": [
                                {"unit": "1/d/{project}", "quotaBuckets": [{"effectiveLimit": str(self.daily_limit)}]}
                            ],
                        },
                    ]
                },
            )

        resource = url.split("/v1/", 1)[1]
        self.calls.append((method, resource, params))

        if resource == f"{self.PARENT}/clients":
            return _HttpResponse(200, {"client": {}}) if self.provisioned else _HttpResponse(404, {"error": {}})
        if resource == f"{self.PARENT}/clients:provision":
            self.provisioned = True
            return _HttpResponse(200)

        listed = re.fullmatch(rf"{self.PARENT}/integrations/([^/:]+)/versions", resource)
        if listed and method == "GET":
            versions = self.versions.get(listed.group(1), [])
            if params and params.get("filter") == "state=ACTIVE":
                versions = [version for version in versions if version["state"] == "ACTIVE"]
            if not versions:
                return _HttpResponse(200, {})
            summaries = [{"name": version["name"], "state": version["state"]} for version in versions]
            return _HttpResponse(200, {"integrationVersions": summaries})
        if listed and method == "POST":
            name = listed.group(1)
            stored = {
                **(json or {}),
                "name": f"{self.STORED_PARENT}/integrations/{name}/versions/v{len(self.versions.get(name, [])) + 1}",
                "state": "DRAFT",
                # Google Cloud 側で追加される項目（定義の比較では無視されるべきもの）
                "eucHardeningDisposition": "HARDENED",
                "integrationParameters": [
                    *(json or {}).get("integrationParameters", []),
                    {"key": "`ExecutionId`", "dataType": "STRING_VALUE", "isTransient": True},
                ],
            }
            self.versions.setdefault(name, []).append(stored)
            return _HttpResponse(200, stored)

        published = re.fullmatch(rf"{self.STORED_PARENT}/integrations/([^/:]+)/versions/([^/:]+):publish", resource)
        if published and method == "POST":
            for version in self.versions[published.group(1)]:
                if version["name"].endswith(f"/{published.group(2)}"):
                    version["state"] = "ACTIVE"
                elif version["state"] == "ACTIVE":
                    version["state"] = "SNAPSHOT"
            return _HttpResponse(200)

        fetched = re.fullmatch(rf"{self.STORED_PARENT}/integrations/([^/:]+)/versions/([^/:]+)", resource)
        if fetched and method == "GET":
            for version in self.versions.get(fetched.group(1), []):
                if version["name"] == resource:
                    return _HttpResponse(200, version)
            return _HttpResponse(404, {"error": {}})

        executed = re.fullmatch(rf"{self.PARENT}/integrations/([^/:]+):execute", resource)
        if executed and method == "POST":
            self.executions.append((executed.group(1), json or {}))
            return _HttpResponse(200, {"executionId": f"exec-{len(self.executions)}"})

        deleted = re.fullmatch(rf"{self.PARENT}/integrations/([^/:]+)", resource)
        if deleted and method == "DELETE":
            self.versions.pop(deleted.group(1), None)
            return _HttpResponse(200)

        return _HttpResponse(400, {"error": {"message": f"unexpected request: {method} {resource}"}})

    def created_versions(self) -> list[dict[str, str] | None]:
        """バージョンを作成した呼び出しごとの、クエリパラメータを返す。"""
        return [params for method, resource, params in self.calls if method == "POST" and resource.endswith("/versions")]


class _PostSession:
    """Application Integration の実行 API への POST を記録する差し替え部品。"""

    def __init__(self, response: _HttpResponse) -> None:
        self.response = response
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, *, json: dict[str, Any], timeout: float) -> _HttpResponse:  # noqa: A002
        self.posts.append((url, json))
        return self.response


def test_integration_definition_fixes_recipients_and_matches_what_the_agent_sends() -> None:
    """宛先が連携の定義に固定され、エージェントが渡す項目と連携が受け取る項目が一致すること。"""
    definition = setup_email_notification.build_integration_version(["sre@example.com", "lead@example.com"])
    task = definition["taskConfigs"][0]
    parameters = task["parameters"]

    assert task["task"] == "EmailTask"
    assert parameters["To"]["value"]["stringArray"]["stringValues"] == ["sre@example.com", "lead@example.com"]
    assert parameters["Cc"]["value"]["stringArray"] == {} and parameters["Bcc"]["value"]["stringArray"] == {}
    assert parameters["Subject"]["value"]["stringValue"] == f"${EMAIL_SUBJECT_PARAM}$"
    assert parameters["TextBody"]["value"]["stringValue"] == f"${EMAIL_BODY_PARAM}$"
    assert parameters["BodyFormat"]["value"]["stringValue"] == "text"
    accepted_inputs = {parameter["key"] for parameter in definition["integrationParameters"]}
    assert accepted_inputs == {EMAIL_SUBJECT_PARAM, EMAIL_BODY_PARAM}  # 宛先は実行時に渡せない

    session = _PostSession(_HttpResponse(200, {"executionId": "exec-1"}))
    settings = AgentSettings(
        project_id="test-proj",
        email_integration_name="sre-notify-email",
        email_integration_location="asia-northeast1",
    )
    ApplicationIntegrationEmailSender(settings, session=session).send(NotificationEmail(subject="件名", body="本文"))

    url, payload = session.posts[0]
    assert url == (
        "https://asia-northeast1-integrations.googleapis.com/v1/projects/test-proj"
        "/locations/asia-northeast1/integrations/sre-notify-email:execute"
    )
    assert payload["triggerId"] == definition["triggerConfigs"][0]["triggerId"] == EMAIL_TRIGGER_ID
    assert set(payload["inputParameters"]) == accepted_inputs
    assert payload["inputParameters"][EMAIL_SUBJECT_PARAM] == {"stringValue": "件名"}


def test_email_sender_reports_failure_only_when_the_api_reports_it() -> None:
    """API がエラーを返した場合と、実行が失敗として終了した場合に、送信失敗として扱うこと。"""
    settings = AgentSettings(project_id="test-proj", email_integration_name="sre-notify-email")
    email = NotificationEmail(subject="s", body="b")

    with pytest.raises(EmailSendError, match="HTTP 403"):
        ApplicationIntegrationEmailSender(settings, session=_PostSession(_HttpResponse(403, {"error": {}}))).send(email)
    with pytest.raises(EmailSendError, match="失敗として終了"):
        ApplicationIntegrationEmailSender(
            settings, session=_PostSession(_HttpResponse(200, {"executionId": "x", "executionFailed": True}))
        ).send(email)
    ApplicationIntegrationEmailSender(settings, session=_PostSession(_HttpResponse(200, {"executionId": "x"}))).send(email)


def test_setup_script_is_idempotent_and_deletes_only_its_integration(capsys: pytest.CaptureFixture[str]) -> None:
    """セットアップを再実行しても重複せず、宛先を変えた時だけ公開し直し、削除は対象の連携だけに行うこと。"""
    service = FakeIntegrationService(provisioned=False)
    service.versions["ExecuteConnection"] = [{"name": "auto-created", "state": "ACTIVE"}]
    base_args = ["--project", "test-proj", "--location", "asia-northeast1"]
    main = setup_email_notification.main

    # 1 回目: リージョンを有効化し、連携を登録して公開し、テストメールを送る
    assert main([*base_args, "--to", "sre@example.com", "--send-test"], session=service) == 0
    first_output = capsys.readouterr().out
    assert "Application Integration を有効化しました" in first_output
    assert "sre-notify-email — 登録して公開しました" in first_output
    assert "メール送信上限は 1 日 5,000 通です" in first_output
    assert service.created_versions() == [{"newIntegration": "true"}]
    assert [version["state"] for version in service.versions["sre-notify-email"]] == ["ACTIVE"]
    assert service.executions[0][0] == "sre-notify-email"
    assert service.executions[0][1]["triggerId"] == EMAIL_TRIGGER_ID

    # 2 回目: 同じ宛先なら何も作らない
    assert main([*base_args, "--to", "sre@example.com"], session=service) == 0
    assert "登録済みの定義と同じため、変更していません" in capsys.readouterr().out
    assert len(service.created_versions()) == 1
    assert len(service.executions) == 1  # --send-test を付けなければ送らない

    # 3 回目: 宛先を変えた時だけ、新しいバージョンを作って公開し直す
    assert main([*base_args, "--to", "sre@example.com,lead@example.com"], session=service) == 0
    assert "新しい定義で公開し直しました" in capsys.readouterr().out
    assert service.created_versions() == [{"newIntegration": "true"}, None]
    assert [version["state"] for version in service.versions["sre-notify-email"]] == ["SNAPSHOT", "ACTIVE"]

    # 削除: 対象の連携だけを消し、有効化時に自動作成された連携には触れない
    assert main([*base_args, "--delete"], session=service) == 0
    assert "sre-notify-email" not in service.versions
    assert "ExecuteConnection" in service.versions
    assert main([*base_args, "--delete"], session=service) == 0
    assert "既に存在しません" in capsys.readouterr().out


def test_setup_script_validates_recipients_and_explains_the_daily_limit(capsys: pytest.CaptureFixture[str]) -> None:
    """宛先の検証と、送信上限の表示（通常・低い・確認できない）を確認する。"""
    parse = setup_email_notification.parse_recipients
    assert parse(" a@example.com, b@example.com ,A@example.com,") == ["a@example.com", "b@example.com"]
    with pytest.raises(ValueError, match="形式が正しくありません"):
        parse("not-an-address")
    with pytest.raises(ValueError, match="宛先がありません"):
        parse(" , ")
    with pytest.raises(ValueError, match="30 件まで"):
        parse(",".join(f"user{i}@example.com" for i in range(31)))

    assert setup_email_notification.main(["--project", "test-proj"], session=FakeIntegrationService()) == 1
    assert "宛先がありません" in capsys.readouterr().err

    read_limit = setup_email_notification.read_email_daily_limit
    describe = setup_email_notification.describe_daily_limit
    assert read_limit(FakeIntegrationService(daily_limit=5000), "test-proj") == 5000
    assert read_limit(FakeIntegrationService(daily_limit=None), "test-proj") is None
    assert describe(5000).startswith("[INFO]") and "5,000 通" in describe(5000)
    assert describe(5).startswith("[WARN]") and "届かなくなる可能性" in describe(5)
    assert "確認できませんでした" in describe(None)


def test_setup_script_status_reports_registration_without_changing_anything(capsys: pytest.CaptureFixture[str]) -> None:
    """--status が登録の有無を終了コードで返し、有効化・登録・送信のどれも行わないこと。"""
    service = FakeIntegrationService(provisioned=False)
    base_args = ["--project", "test-proj", "--location", "asia-northeast1"]
    main = setup_email_notification.main
    not_registered = setup_email_notification.EXIT_NOT_REGISTERED
    assert not_registered not in (0, 1)  # 成功（0）とも失敗（1）とも区別できる終了コードであること

    # リージョンが未有効化: 未登録として返し、有効化しない。宛先を指定しなくても調べられる
    assert main([*base_args, "--status"], session=service) == not_registered
    assert "未登録" in capsys.readouterr().out
    assert service.provisioned is False

    # 作成しただけで公開していない連携は、実行できないため未登録として扱う
    service.provisioned = True
    service.versions["sre-notify-email"] = [{"name": "draft-only", "state": "DRAFT"}]
    assert main([*base_args, "--status"], session=service) == not_registered
    del service.versions["sre-notify-email"]

    # 登録して公開した後: 登録済み。--status は読み取りだけを行う
    assert main([*base_args, "--to", "sre@example.com"], session=service) == 0
    calls_before_status = len(service.calls)
    capsys.readouterr()
    assert main([*base_args, "--status"], session=service) == 0
    assert "登録済み" in capsys.readouterr().out
    status_calls = service.calls[calls_before_status:]
    assert status_calls and all(method == "GET" for method, _, _ in status_calls)
    assert service.executions == []

    # 削除後: 未登録に戻る
    assert main([*base_args, "--delete"], session=service) == 0
    assert main([*base_args, "--status"], session=service) == not_registered


def test_setup_script_registers_nothing_when_the_daily_limit_is_too_low(capsys: pytest.CaptureFixture[str]) -> None:
    """--min-daily-limit を指定すると、送信上限が足りないプロジェクトでは有効化・登録・送信のどれも行わないこと。"""
    base_args = ["--project", "test-proj", "--location", "asia-northeast1", "--to", "sre@example.com", "--send-test"]
    main = setup_email_notification.main
    too_low = setup_email_notification.EXIT_DAILY_LIMIT_TOO_LOW
    assert too_low not in (0, 1, setup_email_notification.EXIT_NOT_REGISTERED)

    # 上限 5 通に対して 8 通が必要: Application Integration の API を 1 度も呼ばずに終わる
    low = FakeIntegrationService(provisioned=False, daily_limit=5)
    assert main([*base_args, "--min-daily-limit", "8"], session=low) == too_low
    assert "1 日 5 通で、必要な 8 通に足りません。何も登録していません。" in capsys.readouterr().err
    assert low.calls == [] and low.provisioned is False and low.versions == {} and low.executions == []

    # メールを 1 通も送れないプロジェクト（上限 0 通）も同じ
    blocked = FakeIntegrationService(daily_limit=0)
    assert main([*base_args, "--min-daily-limit", "8"], session=blocked) == too_low
    assert blocked.calls == []

    # 上限が必要な通数ちょうどなら登録して、テストメールを送る
    exact = FakeIntegrationService(daily_limit=8)
    assert main([*base_args, "--min-daily-limit", "8"], session=exact) == 0
    assert len(exact.executions) == 1

    # 上限を確認できない場合は、登録を続ける
    unknown = FakeIntegrationService(daily_limit=None)
    assert main([*base_args, "--min-daily-limit", "8"], session=unknown) == 0
    assert "sre-notify-email" in unknown.versions

    # --min-daily-limit を指定しなければ、上限が低くても登録して警告を表示する（フル版の動作）
    capsys.readouterr()
    unchecked = FakeIntegrationService(daily_limit=5)
    assert main(base_args, session=unchecked) == 0
    assert "[WARN]" in capsys.readouterr().out
    assert "sre-notify-email" in unchecked.versions


def test_alternative_route_is_disabled_unless_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """メール通知と承認コマンドの待ち受けは、設定しない限り無効であること。"""
    defaults = AgentSettings()
    assert defaults.email_integration_name == ""
    assert defaults.approval_subscription == ""

    monkeypatch.setenv("EMAIL_NOTIFY_INTEGRATION", "sre-notify-email")
    monkeypatch.setenv("APPROVAL_PUBSUB_SUBSCRIPTION", "sre-approval-decisions-sub")
    monkeypatch.setenv("GOOGLE_CLOUD_REGION", "asia-northeast1")
    configured = AgentSettings()
    assert configured.email_integration_name == "sre-notify-email"
    assert configured.email_integration_location == "asia-northeast1"
    assert configured.approval_subscription == "sre-approval-decisions-sub"

"""ADK 2.x Workflow の 3 分岐（静観・自動ロールバック・承認待ち一時停止）結合テスト。"""

from __future__ import annotations

from pathlib import Path

import pytest
from google.adk.sessions import DatabaseSessionService

from agent_app.config import AgentSettings
from agent_app.dedup_store import IncidentGuardrailManager, InMemoryEventStoreBackend
from agent_app.models import AlertEvent, SandboxMetricsAnalysis, SimilarRunbookMatch, TriageDecision
from agent_app.remediation_tools import SafeRemediationService
from agent_app.sandbox_runner import SandboxRunner
from agent_app.vector_kb import LocalJsonlKnowledgeBase
from agent_app.workflow import SreIncidentOrchestrator

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeK8sExecutor:
    """GKE API 呼び出しを記録するテストダブル。"""

    def __init__(self) -> None:
        self.rollbacks: list[tuple[str, str]] = []
        self.restarts: list[tuple[str, str]] = []

    def rollback_deployment(self, namespace: str, deployment_name: str) -> str:
        self.rollbacks.append((namespace, deployment_name))
        return f"Rolled back {namespace}/{deployment_name} to revision 1"

    def restart_deployment(self, namespace: str, deployment_name: str) -> str:
        self.restarts.append((namespace, deployment_name))
        return f"Restarted {namespace}/{deployment_name}"


async def _deterministic_test_analyzer(
    alert: AlertEvent,
    sandbox_stats: SandboxMetricsAnalysis,
    matches: list[SimilarRunbookMatch],
) -> TriageDecision:
    """テスト用に過去インシデント最上位一致とサンドボックス統計から TriageDecision を返す推論器。"""
    top = matches[0]
    if sandbox_stats.is_naturally_decaying or top.recommended_action == "OBSERVE":
        return TriageDecision(
            decision="OBSERVE",
            remediation_type="NONE",
            target_namespace=alert.namespace,
            target_deployment=alert.service_name,
            matched_runbook_id=top.runbook_id,
            similarity_score=top.similarity_score,
            root_cause_hypothesis=top.root_cause,
            rationale="エラー率が自然減衰しており一過性通信瞬断と一致したため静観を選択。",
            anti_pattern_warning=top.anti_patterns,
        )
    return TriageDecision(
        decision=top.recommended_action,
        remediation_type=top.remediation_type,
        target_namespace=alert.namespace,
        target_deployment=alert.service_name,
        matched_runbook_id=top.runbook_id,
        similarity_score=top.similarity_score,
        root_cause_hypothesis=top.root_cause,
        rationale=f"過去事例 {top.runbook_id} ({top.title}) と高類似度で一致。",
        anti_pattern_warning=top.anti_patterns,
    )


@pytest.mark.asyncio
async def test_workflow_three_scenarios_end_to_end(tmp_path: Path) -> None:
    """実 ADK 2.x Workflow + SQLite DatabaseSessionService 上で 3 シナリオすべてが期待通り動作すること。"""
    db_file = tmp_path / "sessions.db"
    settings = AgentSettings(
        sandbox_mode="local_subprocess",
        session_db_url=f"sqlite+aiosqlite:///{db_file}",
    )
    store = InMemoryEventStoreBackend()
    guardrails = IncidentGuardrailManager(settings, store)
    k8s = FakeK8sExecutor()
    remediation = SafeRemediationService(settings, k8s, post_error_rate_probe=lambda _ns, _svc: 0.001)
    kb = LocalJsonlKnowledgeBase(REPO_ROOT / "bigquery" / "seed_runbooks_100.jsonl")

    orchestrator = SreIncidentOrchestrator(
        settings=settings,
        sandbox_runner=SandboxRunner(settings),
        knowledge_base=kb,
        guardrail_manager=guardrails,
        remediation_service=remediation,
        decision_analyzer=_deterministic_test_analyzer,
        session_service=DatabaseSessionService(db_url=settings.session_db_url),
    )

    # 1. シナリオ 1: 一過性ネットワークエラー -> 静観 (GKE 操作 0 回)
    res1 = await orchestrator.handle_alert(
        AlertEvent(
            incident_id="INC-S1",
            error_code="UPSTREAM_CONN_RESET_503",
            summary="Upstream payment gateway connection reset by peer during TLS handshake",
            error_rate_series=[0.45, 0.50, 0.18, 0.03, 0.01],
            recent_deployment_within_30m=False,
        )
    )
    assert res1.decision.decision == "OBSERVE"
    assert res1.paused_for_approval is False
    assert res1.outcome is not None and res1.outcome.action_status == "OBSERVED"
    assert len(k8s.rollbacks) == 0 and len(k8s.restarts) == 0

    # 2. シナリオ 2: 既知バグ v2 デプロイ -> 自動ロールバック実行 + 事後検証 PASS
    res2 = await orchestrator.handle_alert(
        AlertEvent(
            incident_id="INC-S2",
            error_code="CONFIG_KEY_ERROR_500",
            summary="KeyError: PAYMENT_CURRENCY_PRECISION missing after deployment rollout",
            error_rate_series=[0.01, 0.85, 0.90, 0.92],
            recent_deployment_within_30m=True,
        )
    )
    assert res2.decision.decision == "AUTO_REMEDIATE"
    assert res2.paused_for_approval is False
    assert res2.outcome is not None and res2.outcome.action_status == "ROLLED_BACK"
    assert res2.outcome.verification_passed is True
    assert k8s.rollbacks == [("sre-demo-app", "order-service")]

    # 3. シナリオ 3: DB コネクション枯渇 -> @node(require_confirmation=True) で自動停止・承認待ち
    res3 = await orchestrator.handle_alert(
        AlertEvent(
            incident_id="INC-S3",
            error_code="DB_CONN_SLOTS_EXHAUSTED_500",
            summary="OperationalError: FATAL: remaining connection slots are reserved for superuser; active lock wait",
            error_rate_series=[0.20, 0.75, 0.95, 0.98],
            recent_deployment_within_30m=False,
        )
    )
    assert res3.decision.decision == "REQUIRE_APPROVAL"
    assert res3.paused_for_approval is True
    assert res3.confirmation_call_id is not None
    # 承認前は新たなロールバック・再起動が実行されていないこと
    assert len(k8s.rollbacks) == 1 and len(k8s.restarts) == 0

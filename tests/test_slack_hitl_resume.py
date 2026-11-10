"""Slack Socket Mode 承認・却下による ADK 再開（Resume）および pass^k 算出のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest
from google.adk.sessions import DatabaseSessionService

from agent_app.config import AgentSettings
from agent_app.dedup_store import IncidentGuardrailManager, InMemoryEventStoreBackend
from agent_app.models import AlertEvent, SandboxMetricsAnalysis, SimilarRunbookMatch, TriageDecision
from agent_app.remediation_tools import SafeRemediationService
from agent_app.sandbox_runner import SandboxRunner
from agent_app.slack_socket import SlackHitlCoordinator
from agent_app.vector_kb import LocalJsonlKnowledgeBase
from agent_app.workflow import SreIncidentOrchestrator
from calc_pass_hat_k import compute_pass_at_k, compute_pass_hat_k, evaluate_results

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeK8sExecutor:
    def __init__(self) -> None:
        self.restarts: list[tuple[str, str]] = []

    def rollback_deployment(self, namespace: str, deployment_name: str) -> str:
        return f"Rolled back {namespace}/{deployment_name}"

    def restart_deployment(self, namespace: str, deployment_name: str) -> str:
        self.restarts.append((namespace, deployment_name))
        return f"Restarted {namespace}/{deployment_name}"


async def _approval_required_restart_analyzer(
    alert: AlertEvent,
    _stats: SandboxMetricsAnalysis,
    _matches: list[SimilarRunbookMatch],
) -> TriageDecision:
    """承認後の再開実行を検証するため、REQUIRE_APPROVAL + RESTART_DEPLOYMENT を提案する推論器。"""
    return TriageDecision(
        decision="REQUIRE_APPROVAL",
        remediation_type="RESTART_DEPLOYMENT",
        target_namespace=alert.namespace,
        target_deployment=alert.service_name,
        matched_runbook_id="INC-039",
        similarity_score=0.81,
        root_cause_hypothesis="Ephemeral storage saturation requiring operator confirmation before restart",
        rationale="中リスク変更のため人間の Slack 承認を要求します。",
    )


@pytest.mark.asyncio
async def test_slack_hitl_authorization_approve_and_reject_resume(tmp_path: Path) -> None:
    """Slack 承認者チェック、Approve 時の ADK 再開・GKE 実行、Reject 時の実行中止を検証する。"""
    db_file = tmp_path / "hitl_sessions.db"
    settings = AgentSettings(
        sandbox_mode="local_subprocess",
        session_db_url=f"sqlite+aiosqlite:///{db_file}",
        slack_allowed_approvers=("U_SRE_LEAD",),
    )
    store = InMemoryEventStoreBackend()
    guardrails = IncidentGuardrailManager(settings, store)
    k8s = FakeK8sExecutor()
    remediation = SafeRemediationService(settings, k8s, post_error_rate_probe=lambda _ns, _svc: 0.0)
    kb = LocalJsonlKnowledgeBase(REPO_ROOT / "bigquery" / "seed_runbooks_100.jsonl")

    orchestrator = SreIncidentOrchestrator(
        settings=settings,
        sandbox_runner=SandboxRunner(settings),
        knowledge_base=kb,
        guardrail_manager=guardrails,
        remediation_service=remediation,
        decision_analyzer=_approval_required_restart_analyzer,
        session_service=DatabaseSessionService(db_url=settings.session_db_url),
    )
    coordinator = SlackHitlCoordinator(settings, orchestrator)

    # ケース A: 承認待ちチケット作成 -> 権限のないユーザーは拒否 -> 許可ユーザーが Approve して再開実行
    alert_a = AlertEvent(incident_id="HITL-APPROVE-1", summary="Ephemeral storage high", error_rate_series=[0.3, 0.4])
    res_a = await orchestrator.handle_alert(alert_a)
    await coordinator.notify_workflow_result(res_a)
    assert "HITL-APPROVE-1" in coordinator.pending_tickets
    assert len(k8s.restarts) == 0

    with pytest.raises(PermissionError):
        await coordinator.process_approval_decision(
            incident_id="HITL-APPROVE-1",
            slack_user_id="U_INTRUDER",
            approved=True,
        )
    # 拒否された場合はまだチケットが残り、GKE 操作も実行されていないこと
    assert "HITL-APPROVE-1" in coordinator.pending_tickets
    assert len(k8s.restarts) == 0

    outcome_a = await coordinator.process_approval_decision(
        incident_id="HITL-APPROVE-1",
        slack_user_id="U_SRE_LEAD",
        approved=True,
    )
    assert outcome_a.executed is True
    assert outcome_a.action_status == "RESTARTED"
    assert k8s.restarts == [("sre-demo-app", "order-service")]
    assert "HITL-APPROVE-1" not in coordinator.pending_tickets

    # ケース B: Reject が押された場合は GKE 操作を実行せずに REJECTED を記録する
    alert_b = AlertEvent(incident_id="HITL-REJECT-2", summary="Ephemeral storage high", error_rate_series=[0.3, 0.4])
    res_b = await orchestrator.handle_alert(alert_b)
    await coordinator.notify_workflow_result(res_b)

    outcome_b = await coordinator.process_approval_decision(
        incident_id="HITL-REJECT-2",
        slack_user_id="U_SRE_LEAD",
        approved=False,
    )
    assert outcome_b.executed is False
    assert outcome_b.action_status == "REJECTED"
    assert len(k8s.restarts) == 1  # 増えていないこと
    assert any(r.incident_id == "HITL-REJECT-2" and r.decision == "REJECTED" for r in store.rows)


def test_pass_hat_k_blocks_flaky_agent() -> None:
    """5回中4回成功する不安定なエージェント (pass@3=1.0 だが pass^3=0.4) を確実にブロックできること。"""
    assert compute_pass_at_k(n=5, c=4, k=3) == pytest.approx(1.0)
    assert compute_pass_hat_k(n=5, c=4, k=3) == pytest.approx(0.4)

    passed_flaky, scores_flaky = evaluate_results(
        {"transient_observe": [True, True, True, True, False], "known_rollback": [True, True, True, True, True]},
        k=3,
        threshold=0.85,
    )
    assert passed_flaky is False
    assert scores_flaky["transient_observe"] == pytest.approx(0.4)

    passed_solid, _ = evaluate_results(
        {"transient_observe": [True] * 5, "known_rollback": [True] * 5, "unknown_hitl": [True] * 5},
        k=3,
        threshold=0.85,
    )
    assert passed_solid is True

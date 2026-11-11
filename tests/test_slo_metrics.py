"""SLO モニタリング・Error Budget 温存効果計算および前後比較ロジックの単体テスト。"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_app.config import AgentSettings
from agent_app.dedup_store import IncidentGuardrailManager, InMemoryEventStoreBackend
from agent_app.models import AlertEvent, RemediationOutcome, SandboxMetricsAnalysis, TriageDecision
from agent_app.remediation_tools import SafeRemediationService
from agent_app.sandbox_runner import SandboxRunner
from agent_app.slack_socket import build_report_blocks
from agent_app.slo_metrics import (
    DAILY_99_TOTAL_BUDGET_MINUTES,
    MONTHLY_999_TOTAL_BUDGET_MINUTES,
    SloBudgetSnapshot,
    SloWindowStatus,
    calculate_error_budget_impact,
)
from agent_app.vector_kb import LocalJsonlKnowledgeBase
from agent_app.workflow import SreIncidentOrchestrator

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_JSONL = REPO_ROOT / "bigquery" / "seed_runbooks_100.jsonl"


class _FakeSloReader:
    """テスト用の固定 SLO スナップショット読取スタブ。"""

    def __init__(self, daily_pct: float = 91.2, monthly_pct: float = 97.1) -> None:
        self._daily_pct = daily_pct
        self._monthly_pct = monthly_pct

    def read_current_snapshot(self) -> SloBudgetSnapshot:
        return SloBudgetSnapshot(
            project_id="test-proj",
            service_id="order-service-slo",
            daily_slo=SloWindowStatus(
                slo_id="order-availability-daily-99",
                display_name="日次 99.0% 可用性 SLO",
                goal=0.99,
                total_budget_minutes=DAILY_99_TOTAL_BUDGET_MINUTES,
                budget_remaining_pct=self._daily_pct,
                budget_remaining_minutes=round(DAILY_99_TOTAL_BUDGET_MINUTES * self._daily_pct / 100.0, 2),
                compliance_pct=99.91,
                burn_rate_10m=0.0,
            ),
            monthly_slo=SloWindowStatus(
                slo_id="order-availability-monthly-999",
                display_name="月次 99.9% 可用性 SLO",
                goal=0.999,
                total_budget_minutes=MONTHLY_999_TOTAL_BUDGET_MINUTES,
                budget_remaining_pct=self._monthly_pct,
                budget_remaining_minutes=round(MONTHLY_999_TOTAL_BUDGET_MINUTES * self._monthly_pct / 100.0, 2),
                compliance_pct=99.997,
                burn_rate_10m=0.0,
            ),
        )


class _DummyK8sExecutor:
    def rollback_deployment(self, namespace: str, deployment_name: str) -> str:
        return f"Rolled back {namespace}/{deployment_name}"

    def restart_deployment(self, namespace: str, deployment_name: str) -> str:
        return f"Restarted {namespace}/{deployment_name}"


def test_calculate_error_budget_impact_for_auto_remediation() -> None:
    """75秒で自動ロールバック完了した場合、手動30分基準に対し 28.75分のバジェット温存が算出されること。"""
    t0 = datetime(2026, 11, 11, 10, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=75)
    alert = AlertEvent(
        incident_id="inc-slo-1",
        error_code="CONFIG_KEY_ERROR_500",
        summary="KeyError missing PAYMENT_CURRENCY_PRECISION",
        observed_at=t0.isoformat(),
    )
    outcome = RemediationOutcome(
        executed=True,
        action_status="ROLLED_BACK",
        detail_message="Rolled back OK",
        verification_passed=True,
        post_error_rate=0.0,
    )
    snap = _FakeSloReader(daily_pct=91.3, monthly_pct=97.1).read_current_snapshot()
    impact = calculate_error_budget_impact(
        alert=alert,
        decision="AUTO_REMEDIATE",
        outcome=outcome,
        manual_baseline_mttr_minutes=30.0,
        completed_at=t1,
        slo_snapshot=snap,
    )

    assert impact.mttr_seconds == 75.0
    assert impact.estimated_manual_mttr_minutes == 30.0
    assert impact.saved_error_budget_minutes == 28.75
    assert impact.monthly_budget_consumed_pct == 2.9
    assert impact.monthly_budget_saved_pct == 66.6
    assert impact.daily_budget_remaining_pct == 91.3
    assert "28.8分" in impact.impact_summary


def test_calculate_error_budget_impact_does_not_fabricate_savings_on_manual_handoff() -> None:
    """DBロック等の手動引き継ぎケースでは架空の復旧完了 MTTR や温存分数を計上しないこと。"""
    t0 = datetime(2026, 11, 11, 10, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=55)
    alert = AlertEvent(
        incident_id="inc-slo-db",
        error_code="DB_CONN_SLOTS_EXHAUSTED_500",
        summary="DB lock contention",
        observed_at=t0.isoformat(),
    )
    outcome = RemediationOutcome(
        executed=False,
        action_status="MANUAL_HANDOFF_ACKNOWLEDGED",
        detail_message="Handoff to DBA",
        verification_passed=True,
    )
    impact = calculate_error_budget_impact(
        alert=alert,
        decision="APPROVED_EXECUTED",
        outcome=outcome,
        completed_at=t1,
    )

    assert impact.mttr_seconds is None
    assert impact.saved_error_budget_minutes is None
    assert "オンコール担当者へ引継" in impact.impact_summary


async def test_workflow_records_slo_impact_into_event_store_and_slack_blocks(tmp_path: Path) -> None:
    """AUTO_REMEDIATE 完了時に EventStore 監査行と Slack Block Kit の双方へ Error Budget 効果が出力されること。"""
    db_file = tmp_path / "slo_wf.db"
    settings = AgentSettings(
        project_id="test-proj",
        sandbox_mode="local_subprocess",
        session_backend="sqlite",
        session_db_url=f"sqlite+aiosqlite:///{db_file}",
        manual_baseline_mttr_minutes=30.0,
    )
    event_store = InMemoryEventStoreBackend()
    guardrails = IncidentGuardrailManager(settings, event_store)
    kb = LocalJsonlKnowledgeBase(SEED_JSONL)
    rem = SafeRemediationService(settings, _DummyK8sExecutor(), post_error_rate_probe=lambda _n, _s: 0.0)

    async def _fake_analyzer(
        alert: AlertEvent,
        _stats: SandboxMetricsAnalysis,
        _matches: list,
    ) -> TriageDecision:
        return TriageDecision(
            decision="AUTO_REMEDIATE",
            remediation_type="ROLLBACK_DEPLOYMENT",
            target_namespace=alert.namespace,
            target_deployment=alert.service_name,
            matched_runbook_id="INC-001",
            similarity_score=0.95,
            root_cause_hypothesis="環境変数欠落",
            rationale="INC-001 一致により自動ロールバック",
        )

    orchestrator = SreIncidentOrchestrator(
        settings=settings,
        sandbox_runner=SandboxRunner(settings),
        knowledge_base=kb,
        guardrail_manager=guardrails,
        remediation_service=rem,
        decision_analyzer=_fake_analyzer,
        slo_reader=_FakeSloReader(daily_pct=90.5, monthly_pct=96.8),
    )

    t0 = datetime.now(timezone.utc) - timedelta(seconds=60)
    alert = AlertEvent(
        incident_id="inc-wf-slo-1",
        error_code="CONFIG_KEY_ERROR_500",
        summary="KeyError PAYMENT_CURRENCY_PRECISION",
        error_rate_series=[0.0, 0.9, 0.95],
        recent_deployment_within_30m=True,
        observed_at=t0.isoformat(),
    )
    res = await orchestrator.handle_alert(alert)

    assert res.outcome is not None
    assert res.outcome.mttr_seconds is not None and res.outcome.mttr_seconds >= 59.0
    assert res.outcome.saved_error_budget_minutes is not None and res.outcome.saved_error_budget_minutes > 28.0
    assert res.outcome.slo_daily_budget_remaining_pct == 90.5

    assert len(event_store.rows) == 1
    row = event_store.rows[0]
    assert row.saved_error_budget_minutes == res.outcome.saved_error_budget_minutes
    assert row.slo_budget_remaining_pct == 90.5

    blocks = build_report_blocks(res)
    serialized = str(blocks)
    assert "SLO & Error Budget 効果" in serialized
    assert "90.5%" in serialized


def test_check_slo_budget_comparison_formatter() -> None:
    """訓練前後スナップショット比較レポートが実測消費率と温存分数を正しく計算すること。"""
    spec = importlib.util.spec_from_file_location(
        "check_slo_budget",
        REPO_ROOT / "target-app" / "scripts" / "check_slo_budget.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    before = _FakeSloReader(daily_pct=100.0, monthly_pct=100.0).read_current_snapshot()
    after = _FakeSloReader(daily_pct=90.97, monthly_pct=96.99).read_current_snapshot()
    report = mod.format_comparison_report(before, after, manual_mttr_minutes=30.0, observed_bad_minutes_delta=1)

    assert "100.00%  ->  訓練後残量: 90.97%" in report
    assert "実測消費: -9.03%" in report
    assert "自動化により約 28.70 分のバジェットを温存" in report

"""Slack Socket Mode 承認・却下による ADK 再開（Resume）および pass^k 算出のテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest
from google.adk.sessions import DatabaseSessionService

from agent_app.config import AgentSettings
from agent_app.dedup_store import IncidentGuardrailManager, InMemoryEventStoreBackend
from agent_app.models import AlertEvent, RemediationOutcome, SandboxMetricsAnalysis, SimilarRunbookMatch, TriageDecision
from agent_app.remediation_tools import SafeRemediationService
from agent_app.sandbox_runner import SandboxRunner
from agent_app.slack_socket import SlackHitlCoordinator, build_report_blocks
from agent_app.ticket_store import FirestoreApprovalTicketStore
from agent_app.vector_kb import (
    BigQueryVectorKnowledgeBase,
    LocalJsonlKnowledgeBase,
    synthesize_runbook_deterministic,
)
from agent_app.workflow import SreIncidentOrchestrator, WorkflowRunResult, build_session_service
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


async def _fast_det_synthesizer(alert: AlertEvent, decision: TriageDecision, outcome: RemediationOutcome):
    return synthesize_runbook_deterministic(alert, decision, outcome)


@pytest.mark.asyncio
async def test_slack_hitl_authorization_approve_and_reject_resume(tmp_path: Path) -> None:
    """Slack 承認者チェック、Approve 時の ADK 再開・GKE 実行、Reject 時の実行中止を検証する。"""
    db_file = tmp_path / "hitl_sessions.db"
    settings = AgentSettings(
        sandbox_mode="local_subprocess",
        session_backend="sqlite",
        session_db_url=f"sqlite+aiosqlite:///{db_file}",
        slack_bot_token="",
        slack_app_token="",
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
        runbook_synthesizer=_fast_det_synthesizer,
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


def test_slack_report_highlights_failed_post_verification() -> None:
    """自動復旧実行後に事後エラー率 >= 1.0% だった場合、完了ではなく要オンコール対応バッジを出すこと。"""
    alert = AlertEvent(incident_id="POST-NG-1", summary="Error persisted after rollback")
    dec = TriageDecision(
        decision="AUTO_REMEDIATE",
        remediation_type="ROLLBACK_DEPLOYMENT",
        target_namespace="sre-demo-app",
        target_deployment="order-service",
        matched_runbook_id="INC-001",
        similarity_score=0.90,
        root_cause_hypothesis="Suspected release bug",
        rationale="既知バグと一致したためロールバックを実施。",
    )
    failed_out = RemediationOutcome(
        executed=True,
        action_status="ROLLED_BACK",
        detail_message="事後検証 要注意: 操作後もエラー率が 25.00% 残存しています。",
        verification_passed=False,
        post_error_rate=0.25,
    )
    blocks = build_report_blocks(
        WorkflowRunResult(
            alert=alert,
            session_id="s1",
            invocation_id="i1",
            decision=dec,
            paused_for_approval=False,
            outcome=failed_out,
        )
    )
    rendered = str(blocks)
    assert "自動復旧未回復" in rendered
    assert "自動復旧完了" not in rendered


class _FakeSnap:
    def __init__(self, data: dict[str, object] | None) -> None:
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict[str, object] | None:
        return dict(self._data) if self._data is not None else None


class _FakeDocRef:
    def __init__(self, store: dict[str, dict[str, object]], doc_id: str) -> None:
        self._store = store
        self._doc_id = doc_id

    async def set(self, data: dict[str, object]) -> None:
        self._store[self._doc_id] = dict(data)

    async def get(self) -> _FakeSnap:
        return _FakeSnap(self._store.get(self._doc_id))

    async def update(self, fields: dict[str, object]) -> None:
        self._store[self._doc_id].update(fields)

    async def delete(self) -> None:
        self._store.pop(self._doc_id, None)


class _FakeCollectionRef:
    def __init__(self, store: dict[str, dict[str, object]]) -> None:
        self._store = store

    def document(self, doc_id: str) -> _FakeDocRef:
        return _FakeDocRef(self._store, doc_id)


class _FakeFirestoreClient:
    """テスト用の軽量非同期 Firestore フェイククライアント。"""

    def __init__(self) -> None:
        self.collections: dict[str, dict[str, dict[str, object]]] = {}

    def collection(self, name: str) -> _FakeCollectionRef:
        return _FakeCollectionRef(self.collections.setdefault(name, {}))


@pytest.mark.asyncio
async def test_resume_survives_instance_restart_via_persistent_ticket_and_session(tmp_path: Path) -> None:
    """承認待ち中にプロセス/インスタンスが再起動しても、永続化されたセッションとチケットから再開できること。"""
    db_file = tmp_path / "shared_sessions.db"
    fake_fs = _FakeFirestoreClient()
    settings = AgentSettings(
        sandbox_mode="local_subprocess",
        session_backend="sqlite",
        session_db_url=f"sqlite+aiosqlite:///{db_file}",
        slack_allowed_approvers=("U_SRE_LEAD",),
    )
    event_store = InMemoryEventStoreBackend()
    guardrails = IncidentGuardrailManager(settings, event_store)
    k8s = FakeK8sExecutor()
    remediation = SafeRemediationService(settings, k8s, post_error_rate_probe=lambda _ns, _svc: 0.0)
    kb = LocalJsonlKnowledgeBase(REPO_ROOT / "bigquery" / "seed_runbooks_100.jsonl")

    # 1 回目の起動プロセス: 異常検知 -> 承認待ち停止 -> チケットとセッションを永続化
    orch1 = SreIncidentOrchestrator(
        settings=settings,
        sandbox_runner=SandboxRunner(settings),
        knowledge_base=kb,
        guardrail_manager=guardrails,
        remediation_service=remediation,
        decision_analyzer=_approval_required_restart_analyzer,
        runbook_synthesizer=_fast_det_synthesizer,
        session_service=build_session_service(settings),
    )
    coord1 = SlackHitlCoordinator(
        settings,
        orch1,
        ticket_store=FirestoreApprovalTicketStore(settings, client=fake_fs),
    )

    alert = AlertEvent(incident_id="HITL-RESTART-1", summary="DB connection stall", error_rate_series=[0.4, 0.5])
    paused_result = await orch1.handle_alert(alert)
    await coord1.notify_workflow_result(paused_result)
    assert len(k8s.restarts) == 0
    assert "HITL-RESTART-1" in fake_fs.collections[settings.firestore_tickets_collection]

    # 旧プロセスのインスタンスオブジェクトは破棄（インメモリ辞書の消失を模擬）
    del orch1, coord1

    # 2 回目の起動プロセス（新規空メモリ状態で立ち上がった新インスタンス）
    orch2 = SreIncidentOrchestrator(
        settings=settings,
        sandbox_runner=SandboxRunner(settings),
        knowledge_base=kb,
        guardrail_manager=guardrails,
        remediation_service=remediation,
        decision_analyzer=_approval_required_restart_analyzer,
        runbook_synthesizer=_fast_det_synthesizer,
        session_service=build_session_service(settings),
    )
    coord2 = SlackHitlCoordinator(
        settings,
        orch2,
        ticket_store=FirestoreApprovalTicketStore(settings, client=fake_fs),
    )
    assert coord2.pending_tickets == {}  # 新プロセスのローカルメモリは空

    outcome = await coord2.process_approval_decision(
        incident_id="HITL-RESTART-1",
        slack_user_id="U_SRE_LEAD",
        approved=True,
    )
    assert outcome.executed is True
    assert outcome.action_status == "RESTARTED"
    assert k8s.restarts == [("sre-demo-app", "order-service")]
    assert "HITL-RESTART-1" not in fake_fs.collections[settings.firestore_tickets_collection]

    # 二重クリック時は既に消費済みのため KeyError で拒否されること
    with pytest.raises(KeyError):
        await coord2.process_approval_decision(
            incident_id="HITL-RESTART-1",
            slack_user_id="U_SRE_LEAD",
            approved=True,
        )


def test_build_session_service_rejects_unknown_backend() -> None:
    """未知の session_backend 指定時に明示エラーを送出すること。"""
    with pytest.raises(ValueError, match="未対応の session_backend"):
        build_session_service(AgentSettings(session_backend="invalid_backend"))


def test_build_session_service_firestore_returns_adk_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """実 google-cloud-firestore SDK と FirestoreSessionService で初期化できること。"""
    import google.cloud.firestore as fs_mod
    from google.adk.integrations.firestore import FirestoreSessionService
    from google.auth.credentials import AnonymousCredentials

    orig_async_client = fs_mod.AsyncClient
    monkeypatch.setattr(
        fs_mod,
        "AsyncClient",
        lambda project=None, database=None, **kw: orig_async_client(
            project=project or "demo-test",
            database=database or "(default)",
            credentials=AnonymousCredentials(),
        ),
    )

    svc = build_session_service(
        AgentSettings(
            session_backend="firestore",
            project_id="demo-test",
            firestore_database="(default)",
            firestore_sessions_collection="adk-session-test",
        )
    )
    assert isinstance(svc, FirestoreSessionService)


@pytest.mark.asyncio
async def test_autonomous_kb_learning_promotes_novel_restart_to_auto_remediate(tmp_path: Path) -> None:
    """シナリオ4検証: 初見エラーが1回目の承認解決で知識ベースへ自動登録され、2回目は人間承認なしで自動復旧すること。"""
    db_file = tmp_path / "self_learn_sessions.db"
    settings = AgentSettings(
        sandbox_mode="local_subprocess",
        session_backend="sqlite",
        session_db_url=f"sqlite+aiosqlite:///{db_file}",
        enable_autonomous_kb_learning=True,
        allow_drill_cooldown_reset=True,
        slack_allowed_approvers=("U_SRE_LEAD",),
    )
    store = InMemoryEventStoreBackend()
    guardrails = IncidentGuardrailManager(settings, store)
    k8s = FakeK8sExecutor()
    remediation = SafeRemediationService(settings, k8s, post_error_rate_probe=lambda _ns, _svc: 0.002)
    kb = LocalJsonlKnowledgeBase(REPO_ROOT / "bigquery" / "seed_runbooks_100.jsonl")
    assert kb.total_records == 100

    async def _kb_aware_analyzer(
        alert: AlertEvent,
        _stats: SandboxMetricsAnalysis,
        matches: list[SimilarRunbookMatch],
    ) -> TriageDecision:
        top = matches[0]
        if top.similarity_score >= settings.auto_remediate_similarity_threshold:
            return TriageDecision(
                decision=top.recommended_action,
                remediation_type=top.remediation_type,
                target_namespace=alert.namespace,
                target_deployment=alert.service_name,
                matched_runbook_id=top.runbook_id,
                similarity_score=top.similarity_score,
                root_cause_hypothesis=top.root_cause,
                rationale=f"自律学習済み Runbook {top.runbook_id} と一致したため自動実行します。",
            )
        return TriageDecision(
            decision="REQUIRE_APPROVAL",
            remediation_type="RESTART_DEPLOYMENT",
            target_namespace=alert.namespace,
            target_deployment=alert.service_name,
            matched_runbook_id=top.runbook_id,
            similarity_score=top.similarity_score,
            root_cause_hypothesis="Pod 内のインメモリ消費税ルックアップテーブル CRC32 破損",
            rationale="初見エラーのため人間承認後に Deployment ローリング再起動を提案します。",
        )

    orchestrator = SreIncidentOrchestrator(
        settings=settings,
        sandbox_runner=SandboxRunner(settings),
        knowledge_base=kb,
        guardrail_manager=guardrails,
        remediation_service=remediation,
        decision_analyzer=_kb_aware_analyzer,
        runbook_synthesizer=_fast_det_synthesizer,
        session_service=DatabaseSessionService(db_url=settings.session_db_url),
    )
    coordinator = SlackHitlCoordinator(settings, orchestrator)

    # 1. 初回発生: シード 100 件との類似度が < 0.85 のため一時停止し Slack 承認待ちになる
    novel_alert_1 = AlertEvent(
        incident_id="NOVEL-TAX-1",
        error_code="TAX_RULE_CRC32_MISMATCH_500",
        summary="RuntimeError: In-memory consumption tax rate lookup table CRC32 checksum mismatch",
        error_rate_series=[0.15, 0.80, 0.92, 0.95],
        recent_deployment_within_30m=False,
    )
    res_1 = await orchestrator.handle_alert(novel_alert_1)
    assert res_1.paused_for_approval is True
    assert res_1.decision.decision == "REQUIRE_APPROVAL"
    assert res_1.decision.similarity_score < 0.85
    assert len(k8s.restarts) == 0

    await coordinator.notify_workflow_result(res_1)
    outcome_1 = await coordinator.process_approval_decision(
        incident_id="NOVEL-TAX-1",
        slack_user_id="U_SRE_LEAD",
        approved=True,
    )
    assert outcome_1.executed is True
    assert outcome_1.verification_passed is True
    assert outcome_1.learned_runbook_id is not None
    assert outcome_1.learned_runbook_id.startswith("AUTO-")
    assert outcome_1.learned_recommended_action == "AUTO_REMEDIATE"
    assert kb.total_records == 101
    assert len(k8s.restarts) == 1

    # 2. クールダウン解除後、同一初見エラーが再発 -> 今度は AUTO-* に >= 0.85 で一致し承認なしで自動再起動完走！
    guardrails.reset_all_cooldowns("Scenario 4 second injection")
    novel_alert_2 = AlertEvent(
        incident_id="NOVEL-TAX-2",
        error_code="TAX_RULE_CRC32_MISMATCH_500",
        summary="RuntimeError: In-memory consumption tax rate lookup table CRC32 checksum mismatch",
        error_rate_series=[0.20, 0.85, 0.94, 0.96],
        recent_deployment_within_30m=False,
    )
    res_2 = await orchestrator.handle_alert(novel_alert_2)
    assert res_2.paused_for_approval is False
    assert res_2.decision.decision == "AUTO_REMEDIATE"
    assert res_2.decision.matched_runbook_id == outcome_1.learned_runbook_id
    assert res_2.decision.similarity_score >= 0.85
    assert res_2.outcome is not None and res_2.outcome.action_status == "RESTARTED"
    assert len(k8s.restarts) == 2


@pytest.mark.asyncio
async def test_autonomous_kb_learning_keeps_manual_intervention_as_require_approval(tmp_path: Path) -> None:
    """手動介入 (MANUAL_INTERVENTION) 事象は承認解決後も AUTO_REMEDIATE に昇格させず REQUIRE_APPROVAL で登録すること。"""
    db_file = tmp_path / "manual_learn.db"
    settings = AgentSettings(
        sandbox_mode="local_subprocess",
        session_backend="sqlite",
        session_db_url=f"sqlite+aiosqlite:///{db_file}",
        enable_autonomous_kb_learning=True,
        slack_allowed_approvers=("U_SRE_LEAD",),
    )
    store = InMemoryEventStoreBackend()
    guardrails = IncidentGuardrailManager(settings, store)
    k8s = FakeK8sExecutor()
    remediation = SafeRemediationService(settings, k8s, post_error_rate_probe=lambda _ns, _svc: 0.0)
    kb = LocalJsonlKnowledgeBase(REPO_ROOT / "bigquery" / "seed_runbooks_100.jsonl")

    async def _manual_analyzer(
        alert: AlertEvent,
        _stats: SandboxMetricsAnalysis,
        _matches: list[SimilarRunbookMatch],
    ) -> TriageDecision:
        return TriageDecision(
            decision="REQUIRE_APPROVAL",
            remediation_type="MANUAL_INTERVENTION",
            target_namespace=alert.namespace,
            target_deployment=alert.service_name,
            matched_runbook_id="NONE",
            similarity_score=0.35,
            root_cause_hypothesis="外部監査台帳スキーマの非互換変更",
            rationale="手動でのマイグレーション確認が必要です。",
        )

    orchestrator = SreIncidentOrchestrator(
        settings=settings,
        sandbox_runner=SandboxRunner(settings),
        knowledge_base=kb,
        guardrail_manager=guardrails,
        remediation_service=remediation,
        decision_analyzer=_manual_analyzer,
        runbook_synthesizer=_fast_det_synthesizer,
        session_service=DatabaseSessionService(db_url=settings.session_db_url),
    )
    coordinator = SlackHitlCoordinator(settings, orchestrator)

    alert = AlertEvent(
        incident_id="NOVEL-MANUAL-1",
        error_code="LEDGER_SCHEMA_VERSION_MISMATCH_500",
        summary="External audit ledger schema incompatible",
        error_rate_series=[0.5, 0.8],
    )
    res = await orchestrator.handle_alert(alert)
    await coordinator.notify_workflow_result(res)
    outcome = await coordinator.process_approval_decision(
        incident_id="NOVEL-MANUAL-1",
        slack_user_id="U_SRE_LEAD",
        approved=True,
    )
    assert outcome.learned_runbook_id is not None
    assert outcome.learned_recommended_action == "REQUIRE_APPROVAL"
    assert kb.total_records == 101


def test_bigquery_ai_search_and_autonomous_merge_queries() -> None:
    """BigQueryVectorKnowledgeBase が鮮度付き AI.SEARCH・MERGE・鮮度更新・自動降格 SQL を発行すること。"""

    class _FakeRow:
        runbook_id = "AUTO-1234ABCD"
        category = "メモリリーク・FD/PID 枯渇"
        title = "テスト自動登録 Runbook"
        root_cause = "インメモリ破損"
        recommended_action = "AUTO_REMEDIATE"
        remediation_type = "RESTART_DEPLOYMENT"
        remediation_steps = "ローリング再起動"
        anti_patterns = "無検証連打禁止"
        verification_check = "エラー率 < 1.0%"
        last_verified_at_str = "2026-07-01 00:00:00+00"
        verified_age_days = 120.0
        similarity_score = 0.93

    class _FakeJob:
        def __init__(self, rows: list, affected: int = 1) -> None:
            self._rows = rows
            self.num_dml_affected_rows = affected

        def result(self) -> list:
            return self._rows

    class _FakeBqClient:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def query(self, sql: str, job_config=None) -> _FakeJob:  # noqa: ANN001
            self.queries.append(sql)
            return _FakeJob([_FakeRow()], affected=1)

    fake_bq = _FakeBqClient()
    settings = AgentSettings(
        project_id="test-proj",
        bq_dataset="sre_agent_ops",
        runbook_freshness_ttl_days=90,
    )
    bq_kb = BigQueryVectorKnowledgeBase(settings, client=fake_bq)

    alert = AlertEvent(incident_id="BQ-1", error_code="TAX_RULE_CRC32_MISMATCH_500", summary="CRC mismatch")
    matches = bq_kb.search_similar(alert, top_k=3)
    assert len(matches) == 1
    assert matches[0].runbook_id == "AUTO-1234ABCD"
    assert matches[0].verified_age_days == 120.0
    assert matches[0].is_stale is True
    assert "AI.SEARCH(" in fake_bq.queries[0]
    assert "lifecycle_status" in fake_bq.queries[0]
    assert "'search_document'" in fake_bq.queries[0]

    entry = synthesize_runbook_deterministic(
        alert,
        TriageDecision(
            decision="REQUIRE_APPROVAL",
            remediation_type="RESTART_DEPLOYMENT",
            target_namespace="sre-demo-app",
            target_deployment="order-service",
            root_cause_hypothesis="CRC mismatch",
            rationale="Restart needed",
        ),
        RemediationOutcome(executed=True, action_status="RESTARTED", detail_message="OK", verification_passed=True),
    )
    inserted = bq_kb.upsert_learned_runbook(entry)
    assert inserted is True
    assert "MERGE `test-proj.sre_agent_ops.incident_runbooks`" in fake_bq.queries[1]
    assert "last_verified_at" in fake_bq.queries[1]
    assert "embedding" not in fake_bq.queries[1]

    refreshed = bq_kb.refresh_runbook_verification("AUTO-1234ABCD")
    assert refreshed is True
    assert "UPDATE `test-proj.sre_agent_ops.incident_runbooks`" in fake_bq.queries[2]
    assert "last_verified_at = CURRENT_TIMESTAMP()" in fake_bq.queries[2]
    assert "search_document" not in fake_bq.queries[2]

    demoted = bq_kb.demote_runbook_to_approval("AUTO-1234ABCD")
    assert demoted is True
    assert "SET recommended_action = 'REQUIRE_APPROVAL'" in fake_bq.queries[3]
    assert "search_document" not in fake_bq.queries[3]


@pytest.mark.asyncio
async def test_stale_runbook_revalidation_refresh_and_reject_demotion(tmp_path: Path) -> None:
    """鮮度期限切れ (age > TTL) Runbook の承認降格、再承認による鮮度回復、および却下・事後検証NGによる自動降格を検証する。"""
    from agent_app.slack_socket import build_approval_request_blocks

    db_file = tmp_path / "freshness_lifecycle.db"
    settings = AgentSettings(
        sandbox_mode="local_subprocess",
        session_backend="sqlite",
        session_db_url=f"sqlite+aiosqlite:///{db_file}",
        enable_autonomous_kb_learning=True,
        runbook_freshness_ttl_days=90,
        allow_drill_cooldown_reset=True,
        slack_allowed_approvers=("U_SRE_LEAD",),
    )
    store = InMemoryEventStoreBackend()
    guardrails = IncidentGuardrailManager(settings, store)
    k8s = FakeK8sExecutor()
    probe_rate = [0.001]
    remediation = SafeRemediationService(settings, k8s, post_error_rate_probe=lambda _ns, _svc: probe_rate[0])
    kb = LocalJsonlKnowledgeBase(REPO_ROOT / "bigquery" / "seed_runbooks_100.jsonl", freshness_ttl_days=90)

    async def _kb_analyzer(
        alert: AlertEvent,
        _stats: SandboxMetricsAnalysis,
        matches: list[SimilarRunbookMatch],
    ) -> TriageDecision:
        top = matches[0]
        if top.similarity_score >= settings.auto_remediate_similarity_threshold:
            return TriageDecision(
                decision=top.recommended_action,
                remediation_type=top.remediation_type,
                target_namespace=alert.namespace,
                target_deployment=alert.service_name,
                matched_runbook_id=top.runbook_id,
                similarity_score=top.similarity_score,
                root_cause_hypothesis=top.root_cause,
                rationale=f"過去事例 {top.runbook_id} と一致。",
            )
        return TriageDecision(
            decision="REQUIRE_APPROVAL",
            remediation_type="RESTART_DEPLOYMENT",
            target_namespace=alert.namespace,
            target_deployment=alert.service_name,
            matched_runbook_id=top.runbook_id,
            similarity_score=top.similarity_score,
            root_cause_hypothesis="インメモリ税率キャッシュ CRC32 不整合",
            rationale="初見エラーのため承認後に再起動を提案します。",
        )

    orchestrator = SreIncidentOrchestrator(
        settings=settings,
        sandbox_runner=SandboxRunner(settings),
        knowledge_base=kb,
        guardrail_manager=guardrails,
        remediation_service=remediation,
        decision_analyzer=_kb_analyzer,
        runbook_synthesizer=_fast_det_synthesizer,
        session_service=DatabaseSessionService(db_url=settings.session_db_url),
    )
    coordinator = SlackHitlCoordinator(settings, orchestrator)

    def _make_tax_alert(inc_id: str) -> AlertEvent:
        return AlertEvent(
            incident_id=inc_id,
            error_code="TAX_RULE_CRC32_MISMATCH_500",
            summary="RuntimeError: In-memory consumption tax rate lookup table CRC32 checksum mismatch",
            error_rate_series=[0.20, 0.85, 0.93, 0.95],
            recent_deployment_within_30m=False,
        )

    # 1. 初回承認で AUTO-* が登録される (verified_age_days=0.0, verified_count=1)
    r1 = await orchestrator.handle_alert(_make_tax_alert("FRESH-1"))
    await coordinator.notify_workflow_result(r1)
    out1 = await coordinator.process_approval_decision(incident_id="FRESH-1", slack_user_id="U_SRE_LEAD", approved=True)
    auto_id = out1.learned_runbook_id
    assert auto_id is not None
    rec1 = kb.get_runbook_record(auto_id)
    assert rec1 is not None and rec1["verified_age_days"] == 0.0 and rec1["verified_count"] == 1

    # 2. 120日経過に擬似老化させて再注入 -> 類似度 >= 0.85 だが鮮度切れで REQUIRE_APPROVAL へ強制降格！
    kb.set_runbook_age_days(auto_id, 120.0)
    guardrails.reset_all_cooldowns("Age to 120d")
    r2 = await orchestrator.handle_alert(_make_tax_alert("FRESH-2-STALE"))
    assert r2.paused_for_approval is True
    assert r2.decision.decision == "REQUIRE_APPROVAL"
    assert r2.decision.stale_revalidation_required is True
    assert r2.decision.matched_runbook_age_days == pytest.approx(120.0)
    approval_blocks_str = str(build_approval_request_blocks(r2))
    assert "鮮度期限切れ再評価" in approval_blocks_str
    assert "120日前" in approval_blocks_str

    # 3. 人間が再承認 (Approve) -> 処置実行＋回復確認 OK により last_verified_at が 0.0 日前にリフレッシュされる！
    await coordinator.notify_workflow_result(r2)
    out2 = await coordinator.process_approval_decision(
        incident_id="FRESH-2-STALE",
        slack_user_id="U_SRE_LEAD",
        approved=True,
    )
    assert out2.executed is True
    assert out2.refreshed_runbook_id == auto_id
    rec2 = kb.get_runbook_record(auto_id)
    assert rec2 is not None and rec2["verified_age_days"] == 0.0 and rec2["verified_count"] == 2

    # 4. 鮮度回復後の再発 -> 人間承認なしで AUTO_REMEDIATE が直行実行され verified_count が 3 に増える！
    guardrails.reset_all_cooldowns("Fresh again")
    r3 = await orchestrator.handle_alert(_make_tax_alert("FRESH-3-AUTO"))
    assert r3.paused_for_approval is False
    assert r3.decision.decision == "AUTO_REMEDIATE"
    assert r3.outcome is not None and r3.outcome.refreshed_runbook_id == auto_id
    rec3 = kb.get_runbook_record(auto_id)
    assert rec3 is not None and rec3["verified_count"] == 3

    # 5. 再び 150日経過後に人間が Reject した場合 -> Runbook 自体が REQUIRE_APPROVAL へ恒久降格される！
    kb.set_runbook_age_days(auto_id, 150.0)
    guardrails.reset_all_cooldowns("Age to 150d then reject")
    r4 = await orchestrator.handle_alert(_make_tax_alert("FRESH-4-REJECT"))
    assert r4.paused_for_approval is True
    await coordinator.notify_workflow_result(r4)
    out4 = await coordinator.process_approval_decision(
        incident_id="FRESH-4-REJECT",
        slack_user_id="U_SRE_LEAD",
        approved=False,
    )
    assert out4.action_status == "REJECTED"
    assert out4.demoted_runbook_id == auto_id
    rec4 = kb.get_runbook_record(auto_id)
    assert rec4 is not None and rec4["recommended_action"] == "REQUIRE_APPROVAL"

    # 6. 既存の鮮度内 AUTO_REMEDIATE (INC-001) でも事後検証 NG (エラー率残存) の場合は自動で REQUIRE_APPROVAL へ降格される！
    guardrails.reset_all_cooldowns("Post probe failure demotion")
    probe_rate[0] = 0.45  # エラー率 45% 残存 = 事後検証 NG
    r5 = await orchestrator.handle_alert(
        AlertEvent(
            incident_id="FRESH-5-PROBE-NG",
            error_code="CONFIG_KEY_ERROR_500",
            summary="KeyError: PAYMENT_CURRENCY_PRECISION missing after deployment rollout",
            error_rate_series=[0.10, 0.88, 0.92],
            recent_deployment_within_30m=True,
        )
    )
    assert r5.decision.decision == "AUTO_REMEDIATE"
    assert r5.outcome is not None and r5.outcome.verification_passed is False
    assert r5.outcome.demoted_runbook_id == "INC-001"
    inc001 = kb.get_runbook_record("INC-001")
    assert inc001 is not None and inc001["recommended_action"] == "REQUIRE_APPROVAL"


@pytest.mark.asyncio
async def test_slack_button_click_immediately_updates_to_in_progress_and_restores_on_error(tmp_path: Path) -> None:
    """ボタン押下直後に『⏳ 承認受付・実行中...』(ボタン消去) へ更新し、完了時に最終更新・例外時にボタン復元すること。"""
    from types import SimpleNamespace

    db_file = tmp_path / "ux_progress.db"
    settings = AgentSettings(
        sandbox_mode="local_subprocess",
        session_backend="sqlite",
        session_db_url=f"sqlite+aiosqlite:///{db_file}",
        slack_channel_id="C_TEST_CH",
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
        runbook_synthesizer=_fast_det_synthesizer,
        session_service=DatabaseSessionService(db_url=settings.session_db_url),
    )
    coordinator = SlackHitlCoordinator(settings, orchestrator)

    chat_updates: list[dict] = []

    class _FakeSlackClient:
        async def chat_postMessage(self, **kwargs):  # noqa: ANN003, N802
            return {"channel": "C_TEST_CH", "ts": "1700000000.000100"}

        async def chat_update(self, **kwargs):  # noqa: ANN003
            # chat_update 呼び出し時点での GKE 再起動回数も一緒に記録し、実行『前』に1回目が呼ばれたことを証明する
            chat_updates.append({"kwargs": kwargs, "restarts_at_call_time": len(k8s.restarts)})
            return {"ok": True}

    coordinator._bolt_app = SimpleNamespace(client=_FakeSlackClient())  # type: ignore[assignment]

    # 1. 正常系: 1回目 chat_update (restarts=0, ⏳ 承認受付・実行中..., actions なし) -> 2回目 chat_update (restarts=1, ✅ 承認・実行完了)
    res_ok = await orchestrator.handle_alert(
        AlertEvent(incident_id="UX-OK-1", summary="Restart test", error_rate_series=[0.4, 0.5])
    )
    await coordinator.notify_workflow_result(res_ok)
    await coordinator.process_approval_decision(incident_id="UX-OK-1", slack_user_id="U_SRE_LEAD", approved=True)

    assert len(chat_updates) == 2
    first_blocks = chat_updates[0]["kwargs"]["blocks"]
    assert chat_updates[0]["restarts_at_call_time"] == 0
    assert "⏳ 承認受付・実行中..." in str(first_blocks)
    assert all(b.get("type") != "actions" for b in first_blocks)

    second_blocks = chat_updates[1]["kwargs"]["blocks"]
    assert chat_updates[1]["restarts_at_call_time"] == 1
    assert "✅ 承認・実行完了" in str(second_blocks)

    # 2. 異常系: resume_from_approval が一時例外を送出した場合、チケット解放と同時に承認ボタン付きカードへ復元されること
    chat_updates.clear()
    res_err = await orchestrator.handle_alert(
        AlertEvent(incident_id="UX-ERR-2", summary="Transient error test", error_rate_series=[0.4, 0.5])
    )
    await coordinator.notify_workflow_result(res_err)

    orig_resume = orchestrator.resume_from_approval

    async def _failing_resume(**_kw):  # noqa: ANN003
        raise RuntimeError("Transient network timeout")

    orchestrator.resume_from_approval = _failing_resume  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="Transient network timeout"):
            await coordinator.process_approval_decision(
                incident_id="UX-ERR-2",
                slack_user_id="U_SRE_LEAD",
                approved=True,
            )
    finally:
        orchestrator.resume_from_approval = orig_resume  # type: ignore[method-assign]

    assert len(chat_updates) == 2
    assert "⏳ 承認受付・実行中..." in str(chat_updates[0]["kwargs"]["blocks"])
    restored_blocks = chat_updates[1]["kwargs"]["blocks"]
    assert any(b.get("type") == "actions" for b in restored_blocks)





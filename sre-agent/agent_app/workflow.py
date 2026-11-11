"""ADK 2.x グラフワークフロー定義 (`workflow.py`)。

常駐 SRE エージェントの調査・相関分析・ガードレール検証・自動復旧・Slack 承認一時停止を
ADK 2.x の `Workflow` / `JoinNode` / `@node(require_confirmation=True)` で制御します。

グラフ構造:
  START
    ├──> [analyze_metrics_in_sandbox]  (Cloud Run sandbox 内でエラー率の減衰傾きを計算)
    └──> [search_similar_incidents]    (BigQuery VECTOR_SEARCH で過去障害 100 件を照合)
            │
            ▼
       [JoinNode: join_diagnostics]
            │
            ▼
       [correlate_and_decide]          (Gemini 3.8 Flash + 公式リモート MCP で根本原因と方針を推論)
            │
            ▼
       [enforce_guardrails_and_route]  (Namespace 制限・類似度下限・1時間1回上限を適用し3分岐)
            ├── "OBSERVE"          ──> [observe_node]
            ├── "AUTO_REMEDIATE"   ──> [auto_remediate_node]
            └── "REQUIRE_APPROVAL" ──> [approved_remediate_node (require_confirmation=True)]
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from google.adk import Agent, Context, Event, Runner
from google.adk.apps import App, ResumabilityConfig
from google.adk.models.google_llm import Gemini
from google.adk.sessions import BaseSessionService, DatabaseSessionService
from google.adk.workflow import JoinNode, RetryConfig, START, Workflow, node
from google.genai import types

from .config import AgentSettings
from .dedup_store import IncidentGuardrailManager
from .mcp_tools import build_observability_mcp_toolsets
from .models import (
    AlertEvent,
    RemediationOutcome,
    SandboxMetricsAnalysis,
    SimilarRunbookMatch,
    TriageDecision,
)
from .remediation_tools import SafeRemediationService
from .sandbox_runner import SandboxRunner
from .slo_metrics import SloReader, calculate_error_budget_impact
from .vector_kb import (
    RunbookKnowledgeBase,
    RunbookSynthesizerFn,
    build_llm_runbook_synthesizer,
)

DecisionAnalyzerFn = Callable[
    [AlertEvent, SandboxMetricsAnalysis, list[SimilarRunbookMatch]],
    Awaitable[TriageDecision],
]
TelemetryEnricherFn = Callable[[AlertEvent], Awaitable[AlertEvent]]


def _emit_step_log(message: str, **extra: Any) -> None:
    sys.stdout.write(json.dumps({"severity": "INFO", "message": message, **extra}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


@dataclass
class WorkflowRunResult:
    """1 回のインシデント処理実行（または承認待ち一時停止）の結果。"""

    alert: AlertEvent
    session_id: str
    invocation_id: str
    decision: TriageDecision
    paused_for_approval: bool
    confirmation_call_id: str | None = None
    outcome: RemediationOutcome | None = None


def build_llm_decision_analyzer(
    settings: AgentSettings,
    *,
    enable_remote_mcp: bool = True,
) -> DecisionAnalyzerFn:
    """Gemini 3.8 Flash と公式リモート MCP 読み取りツール群を用いた本番用トリアージ推論器を構築する。"""
    tools: list[Any] = build_observability_mcp_toolsets(settings) if enable_remote_mcp else []

    triage_agent = Agent(
        name="sre_correlation_analyst",
        model=Gemini(
            model=settings.triage_model,
            retry_options=types.HttpRetryOptions(attempts=3),
        ),
        instruction=(
            "あなたは本番 GKE システムを担当するシニア SRE エージェントです。\n"
            "与えられたアラート情報、隔離サンドボックスで計算したエラー率の減衰傾き、"
            "および過去インシデント知識ベースの類似事例（上位3件）を突き合わせ、"
            "必要に応じて読み取り専用の Monitoring / Logging / GKE ツールで直近状況を確認した上で、"
            "以下の基準で厳密に TriageDecision スキーマを出力してください。\n\n"
            "1. 最上位の過去類似事例（類似度 >= 0.85）が recommended_action='REQUIRE_APPROVAL'（DB ロック競合・スキーマ不整合等）の場合、"
            "または過去事例との類似度が 0.85 未満の初見持続エラー（is_naturally_decaying=False）の場合:\n"
            "   -> 必ず decision='REQUIRE_APPROVAL' とし、勝手に本番変更も放置静観もせず人間の判断を仰ぐ。\n"
            "      その際、インメモリキャッシュ破損・チェックサム不整合（CRC32 mismatch / heap cache corrupted）・メモリリークなど "
            "稼働中ワーカーのメモリ状態異常で Pod 再起動による初期化回復が見込める場合は必ず remediation_type='RESTART_DEPLOYMENT' を提案し、\n"
            "      直近の新規デプロイ（recent_deployment_within_30m=True）で環境変数欠落や起動バグが混入した場合のみ 'ROLLBACK_DEPLOYMENT'、"
            "      DB ロック・永続データ不整合・外部システム障害の場合は 'MANUAL_INTERVENTION' を指定する。\n"
            "2. 一過性通信瞬断などで実トラフィック上のエラー率が既に自然減衰（is_naturally_decaying=True）しており、"
            "かつ過去類似事例が OBSERVE 推奨である場合:\n"
            "   -> decision='OBSERVE', remediation_type='NONE' とする（不要な再起動でコネクション再確立嵐を起こさないこと）。\n"
            "3. 直近デプロイ起因の既知バグや既知のインメモリキャッシュ破損・Stateless リソース肥大化で、AUTO_REMEDIATE 推奨の過去事例と強く一致（>= 0.85）し持続エラー（is_naturally_decaying=False）である場合:\n"
            "   -> decision='AUTO_REMEDIATE' とし、remediation_type に過去事例と同じ 'ROLLBACK_DEPLOYMENT' または 'RESTART_DEPLOYMENT' を指定する"
            "（Runbook の鮮度有効期限チェックや1時間回数上限は後続の決定論的ガードレールが自動適用するため、ここでは過去事例の推奨区分に従って出力すること）。"
        ),
        tools=tools,
        output_schema=TriageDecision,
    )
    app = App(name="sre_triage_subapp", root_agent=triage_agent)

    async def _analyze(
        alert: AlertEvent,
        sandbox_stats: SandboxMetricsAnalysis,
        similar_matches: list[SimilarRunbookMatch],
    ) -> TriageDecision:
        from google.adk.sessions import InMemorySessionService

        mem_sessions = InMemorySessionService()
        runner = Runner(app=app, session_service=mem_sessions)
        sess = await mem_sessions.create_session(app_name="sre_triage_subapp", user_id="sre_orchestrator")

        prompt = json.dumps(
            {
                "environment": {
                    "project_id": settings.project_id,
                    "gke_cluster_name": settings.gke_cluster_name,
                    "gke_cluster_location": settings.gke_cluster_location,
                },
                "alert": alert.model_dump(),
                "sandbox_metrics_analysis": sandbox_stats.model_dump(),
                "similar_past_incidents": [m.model_dump() for m in similar_matches],
            },
            ensure_ascii=False,
            indent=2,
        )
        last_text = ""
        async for ev in runner.run_async(
            user_id="sre_orchestrator",
            session_id=sess.id,
            new_message=types.Content(role="user", parts=[types.Part.from_text(text=prompt)]),
        ):
            if ev.content and ev.content.parts:
                for part in ev.content.parts:
                    if part.text and not getattr(part, "thought", False):
                        last_text = part.text

        if not last_text:
            raise RuntimeError("トリアージ推論モデルから有効な JSON 応答が得られませんでした。")
        return TriageDecision.model_validate_json(last_text)

    return _analyze


def build_session_service(settings: AgentSettings) -> BaseSessionService:
    """設定に応じて ADK 2.x セッション永続化バックエンド（Firestore または SQLite）を生成する。"""
    backend = settings.session_backend.strip().lower()
    if backend == "firestore":
        from google.adk.integrations.firestore import FirestoreSessionService
        from google.cloud import firestore

        client = firestore.AsyncClient(
            project=settings.project_id,
            database=settings.firestore_database,
        )
        return FirestoreSessionService(
            client=client,
            root_collection=settings.firestore_sessions_collection,
        )
    if backend == "sqlite":
        return DatabaseSessionService(db_url=settings.session_db_url)
    raise ValueError(f"未対応の session_backend です: {settings.session_backend!r}")


class SreIncidentOrchestrator:
    """ADK 2.x `Workflow` を用いたインシデント調査・自動復旧・HITL 再開オーケストレーター。"""

    APP_NAME = "agentic_sre_workflow_app"
    USER_ID = "sre_daemon"

    def __init__(
        self,
        *,
        settings: AgentSettings,
        sandbox_runner: SandboxRunner,
        knowledge_base: RunbookKnowledgeBase,
        guardrail_manager: IncidentGuardrailManager,
        remediation_service: SafeRemediationService,
        decision_analyzer: DecisionAnalyzerFn | None = None,
        telemetry_enricher: TelemetryEnricherFn | None = None,
        runbook_synthesizer: RunbookSynthesizerFn | None = None,
        session_service: BaseSessionService | None = None,
        slo_reader: SloReader | None = None,
    ) -> None:
        self._settings = settings
        self._sandbox = sandbox_runner
        self._kb = knowledge_base
        self._guardrails = guardrail_manager
        self._remediation = remediation_service
        self._analyzer = decision_analyzer or build_llm_decision_analyzer(settings)
        self._enricher = telemetry_enricher
        self._synthesizer = runbook_synthesizer or build_llm_runbook_synthesizer(settings)
        self._sessions = session_service or build_session_service(settings)
        self._slo_reader = slo_reader
        self._app = self._build_adk_app()
        self._runner = Runner(app=self._app, session_service=self._sessions)

    async def _enrich_outcome_with_slo(
        self,
        alert: AlertEvent,
        decision_label: str,
        outcome: RemediationOutcome,
    ) -> RemediationOutcome:
        """処置結果に実測 MTTR・Saved Error Budget・現在の Cloud Monitoring SLO 残量を付与する。"""
        snapshot = (
            await asyncio.to_thread(self._slo_reader.read_current_snapshot)
            if self._slo_reader is not None
            else None
        )
        impact = calculate_error_budget_impact(
            alert=alert,
            decision=decision_label,
            outcome=outcome,
            manual_baseline_mttr_minutes=self._settings.manual_baseline_mttr_minutes,
            slo_snapshot=snapshot,
        )
        return outcome.model_copy(
            update={
                "mttr_seconds": impact.mttr_seconds,
                "estimated_manual_mttr_minutes": impact.estimated_manual_mttr_minutes,
                "saved_error_budget_minutes": impact.saved_error_budget_minutes,
                "monthly_budget_saved_pct": impact.monthly_budget_saved_pct,
                "slo_daily_budget_remaining_pct": impact.daily_budget_remaining_pct,
                "slo_monthly_budget_remaining_pct": impact.monthly_budget_remaining_pct,
                "slo_burn_rate_10m": impact.burn_rate_10m,
                "slo_impact_summary": impact.impact_summary,
            }
        )

    def _build_adk_app(self) -> App:
        settings = self._settings
        sandbox = self._sandbox
        kb = self._kb
        analyzer = self._analyzer
        guardrails = self._guardrails
        remediation = self._remediation

        @node(
            name="analyze_metrics_in_sandbox",
            timeout=75.0,
            retry_config=RetryConfig(max_attempts=2, initial_delay=0.5),
        )
        async def analyze_metrics_in_sandbox(ctx: Context) -> dict[str, Any]:
            alert = AlertEvent.model_validate(ctx.state["alert"])
            stats = await asyncio.to_thread(sandbox.analyze_error_series, alert.error_rate_series)
            ctx.state["sandbox_stats"] = stats.model_dump()
            _emit_step_log(
                "隔離サンドボックス時系列解析を完了しました。",
                incident_id=alert.incident_id,
                error_rate_series=alert.error_rate_series,
                recent_deployment_within_30m=alert.recent_deployment_within_30m,
                sandbox_stats=stats.model_dump(),
            )
            return stats.model_dump()

        @node(
            name="search_similar_incidents",
            timeout=20.0,
            retry_config=RetryConfig(max_attempts=3, initial_delay=1.0),
        )
        async def search_similar_incidents(ctx: Context) -> list[dict[str, Any]]:
            alert = AlertEvent.model_validate(ctx.state["alert"])
            matches = await asyncio.to_thread(kb.search_similar, alert, 3)
            dumped = [m.model_dump() for m in matches]
            ctx.state["similar_matches"] = dumped
            return dumped

        join_diagnostics = JoinNode(name="join_diagnostics")

        @node(name="correlate_and_decide", timeout=45.0)
        async def correlate_and_decide(ctx: Context) -> dict[str, Any]:
            alert = AlertEvent.model_validate(ctx.state["alert"])
            stats = SandboxMetricsAnalysis.model_validate(ctx.state["sandbox_stats"])
            matches = [SimilarRunbookMatch.model_validate(m) for m in ctx.state["similar_matches"]]
            raw_decision = await analyzer(alert, stats, matches)
            ctx.state["raw_decision"] = raw_decision.model_dump()
            _emit_step_log(
                "Gemini トリアージ推論を完了しました。",
                incident_id=alert.incident_id,
                decision=raw_decision.decision,
                matched_runbook_id=raw_decision.matched_runbook_id,
                similarity_score=raw_decision.similarity_score,
            )
            return raw_decision.model_dump()

        @node(name="enforce_guardrails_and_route")
        async def enforce_guardrails_and_route(ctx: Context) -> Event:
            alert = AlertEvent.model_validate(ctx.state["alert"])
            decision = TriageDecision.model_validate(ctx.state["raw_decision"])
            matches = [SimilarRunbookMatch.model_validate(m) for m in ctx.state.get("similar_matches", [])]
            matched_match = next(
                (m for m in matches if m.runbook_id == decision.matched_runbook_id),
                None,
            )
            if (
                matched_match is None
                and matches
                and decision.similarity_score >= settings.auto_remediate_similarity_threshold
            ):
                matched_match = matches[0]
            matched_age = matched_match.verified_age_days if matched_match is not None else None

            safe_decision, safe_rem, downgrade_reason = await asyncio.to_thread(
                guardrails.enforce_safety_guardrails,
                namespace=alert.namespace,
                service_name=alert.service_name,
                proposed_decision=decision.decision,
                proposed_remediation=decision.remediation_type,
                similarity_score=decision.similarity_score,
                matched_runbook_age_days=matched_age,
            )
            stale_revalidation = bool(
                safe_decision == "REQUIRE_APPROVAL"
                and matched_match is not None
                and matched_match.recommended_action == "AUTO_REMEDIATE"
                and decision.similarity_score >= settings.auto_remediate_similarity_threshold
                and matched_age is not None
                and matched_age > float(settings.runbook_freshness_ttl_days)
            )
            if stale_revalidation and not downgrade_reason:
                downgrade_reason = (
                    f"一致した過去 Runbook ({matched_match.runbook_id}) の最終検証から {matched_age:.1f} 日が経過しており、"
                    f"鮮度有効期限 ({settings.runbook_freshness_ttl_days} 日) を超過しているため人間による再評価・承認を要求します。"
                )

            rationale = (
                f"{decision.rationale} [ガードレール注記: {downgrade_reason}]"
                if downgrade_reason
                else decision.rationale
            )
            final_decision = decision.model_copy(
                update={
                    "decision": safe_decision,
                    "remediation_type": safe_rem,
                    "target_namespace": alert.namespace,
                    "target_deployment": alert.service_name,
                    "rationale": rationale,
                    "matched_runbook_age_days": matched_age if matched_age is not None else -1.0,
                    "stale_revalidation_required": stale_revalidation,
                }
            )
            ctx.state["final_decision"] = final_decision.model_dump()
            _emit_step_log(
                "安全ガードレール検証・ルーティング判定を完了しました。",
                incident_id=alert.incident_id,
                final_route=final_decision.decision,
                remediation_type=final_decision.remediation_type,
                matched_runbook_age_days=final_decision.matched_runbook_age_days,
                stale_revalidation_required=final_decision.stale_revalidation_required,
                downgrade_reason=downgrade_reason,
            )
            return Event(output=final_decision.model_dump(), route=final_decision.decision)

        @node(name="observe_node")
        async def observe_node(ctx: Context) -> dict[str, Any]:
            alert = AlertEvent.model_validate(ctx.state["alert"])
            decision = TriageDecision.model_validate(ctx.state["final_decision"])
            outcome = RemediationOutcome(
                executed=False,
                action_status="OBSERVED",
                detail_message="一過性事象または静観対象と判定されたため、Pod 再起動・ロールバックを行わず静観しました。",
                verification_passed=True,
            )
            outcome = await self._enrich_outcome_with_slo(alert, "OBSERVE", outcome)
            await asyncio.to_thread(
                guardrails.record_outcome,
                alert=alert,
                decision="OBSERVE",
                remediation_type="NONE",
                matched_runbook_id=decision.matched_runbook_id,
                similarity_score=decision.similarity_score,
                verification_passed=True,
                summary=decision.rationale,
                mttr_seconds=outcome.mttr_seconds,
                estimated_manual_mttr_minutes=outcome.estimated_manual_mttr_minutes,
                saved_error_budget_minutes=outcome.saved_error_budget_minutes,
                slo_budget_remaining_pct=outcome.slo_daily_budget_remaining_pct,
            )
            ctx.state["outcome"] = outcome.model_dump()
            return outcome.model_dump()

        @node(name="auto_remediate_node")
        async def auto_remediate_node(ctx: Context) -> dict[str, Any]:
            alert = AlertEvent.model_validate(ctx.state["alert"])
            decision = TriageDecision.model_validate(ctx.state["final_decision"])
            outcome = await asyncio.to_thread(
                remediation.execute_and_verify,
                namespace=decision.target_namespace,
                deployment_name=decision.target_deployment,
                remediation_type=decision.remediation_type,
            )
            if (
                outcome.verification_passed
                and decision.matched_runbook_id != "NONE"
                and hasattr(kb, "refresh_runbook_verification")
            ):
                refreshed = await asyncio.to_thread(
                    kb.refresh_runbook_verification,
                    decision.matched_runbook_id,
                )
                if refreshed:
                    outcome = outcome.model_copy(
                        update={
                            "refreshed_runbook_id": decision.matched_runbook_id,
                            "detail_message": (
                                f"{outcome.detail_message} | "
                                f"Runbook {decision.matched_runbook_id} の最終検証日時 (last_verified_at) を更新しました"
                            ),
                        }
                    )
            elif (
                not outcome.verification_passed
                and decision.matched_runbook_id != "NONE"
                and hasattr(kb, "demote_runbook_to_approval")
            ):
                demoted = await asyncio.to_thread(
                    kb.demote_runbook_to_approval,
                    decision.matched_runbook_id,
                )
                if demoted:
                    outcome = outcome.model_copy(
                        update={
                            "demoted_runbook_id": decision.matched_runbook_id,
                            "detail_message": (
                                f"{outcome.detail_message} | "
                                f"事後検証 NG のため Runbook {decision.matched_runbook_id} を REQUIRE_APPROVAL へ自動降格しました"
                            ),
                        }
                    )

            outcome = await self._enrich_outcome_with_slo(alert, "AUTO_REMEDIATE", outcome)
            await asyncio.to_thread(
                guardrails.record_outcome,
                alert=alert,
                decision="AUTO_REMEDIATE",
                remediation_type=decision.remediation_type,
                matched_runbook_id=decision.matched_runbook_id,
                similarity_score=decision.similarity_score,
                verification_passed=outcome.verification_passed,
                summary=f"{decision.rationale} | {outcome.detail_message}",
                mttr_seconds=outcome.mttr_seconds,
                estimated_manual_mttr_minutes=outcome.estimated_manual_mttr_minutes,
                saved_error_budget_minutes=outcome.saved_error_budget_minutes,
                slo_budget_remaining_pct=outcome.slo_daily_budget_remaining_pct,
            )
            ctx.state["outcome"] = outcome.model_dump()
            return outcome.model_dump()

        @node(name="approved_remediate_node", require_confirmation=True)
        async def approved_remediate_node(ctx: Context) -> dict[str, Any]:
            """Slack の Interactive 承認ボタンで Approve が押された後にのみ本体が実行されるノード。"""
            alert = AlertEvent.model_validate(ctx.state["alert"])
            decision = TriageDecision.model_validate(ctx.state["final_decision"])

            if decision.remediation_type in ("ROLLBACK_DEPLOYMENT", "RESTART_DEPLOYMENT"):
                outcome = await asyncio.to_thread(
                    remediation.execute_and_verify,
                    namespace=decision.target_namespace,
                    deployment_name=decision.target_deployment,
                    remediation_type=decision.remediation_type,
                )
            else:
                outcome = RemediationOutcome(
                    executed=False,
                    action_status="MANUAL_HANDOFF_ACKNOWLEDGED",
                    detail_message="オンコール担当者が手動調査・対処を引き継ぎ承認しました。",
                    verification_passed=True,
                )

            outcome = await self._enrich_outcome_with_slo(alert, "APPROVED_EXECUTED", outcome)
            await asyncio.to_thread(
                guardrails.record_outcome,
                alert=alert,
                decision="APPROVED_EXECUTED",
                remediation_type=decision.remediation_type,
                matched_runbook_id=decision.matched_runbook_id,
                similarity_score=decision.similarity_score,
                verification_passed=outcome.verification_passed,
                summary=f"[人間承認済み] {outcome.detail_message}",
                mttr_seconds=outcome.mttr_seconds,
                estimated_manual_mttr_minutes=outcome.estimated_manual_mttr_minutes,
                saved_error_budget_minutes=outcome.saved_error_budget_minutes,
                slo_budget_remaining_pct=outcome.slo_daily_budget_remaining_pct,
            )
            ctx.state["outcome"] = outcome.model_dump()
            return outcome.model_dump()

        workflow = Workflow(
            name="agentic_sre_incident_workflow",
            edges=[
                (
                    START,
                    (analyze_metrics_in_sandbox, search_similar_incidents),
                    join_diagnostics,
                    correlate_and_decide,
                    enforce_guardrails_and_route,
                    {
                        "OBSERVE": observe_node,
                        "AUTO_REMEDIATE": auto_remediate_node,
                        "REQUIRE_APPROVAL": approved_remediate_node,
                    },
                )
            ],
        )
        return App(
            name=self.APP_NAME,
            root_agent=workflow,
            resumability_config=ResumabilityConfig(is_resumable=True),
        )

    async def handle_alert(self, alert: AlertEvent) -> WorkflowRunResult:
        """新規アラートに対してテレメトリ補完後にワークフローを起動する（承認が必要な場合は一時停止状態で返る）。"""
        if self._enricher is not None:
            alert = await self._enricher(alert)

        session = await self._sessions.create_session(
            app_name=self.APP_NAME,
            user_id=self.USER_ID,
            state={"alert": alert.model_dump()},
        )

        invocation_id = ""
        confirmation_call_id: str | None = None

        async for ev in self._runner.run_async(
            user_id=self.USER_ID,
            session_id=session.id,
            new_message=types.Content(
                role="user",
                parts=[types.Part.from_text(text=f"Investigate incident {alert.incident_id}")],
            ),
        ):
            invocation_id = ev.invocation_id
            for fcall in ev.get_function_calls():
                if fcall.name == "adk_request_input":
                    confirmation_call_id = fcall.id

        updated_session = await self._sessions.get_session(
            app_name=self.APP_NAME,
            user_id=self.USER_ID,
            session_id=session.id,
        )
        state = updated_session.state if updated_session else {}
        final_decision = TriageDecision.model_validate(state["final_decision"])

        if confirmation_call_id:
            await asyncio.to_thread(
                self._guardrails.record_outcome,
                alert=alert,
                decision="REQUIRE_APPROVAL",
                remediation_type=final_decision.remediation_type,
                matched_runbook_id=final_decision.matched_runbook_id,
                similarity_score=final_decision.similarity_score,
                verification_passed=False,
                summary=f"[Slack 承認待機] {final_decision.rationale}",
            )
            return WorkflowRunResult(
                alert=alert,
                session_id=session.id,
                invocation_id=invocation_id,
                decision=final_decision,
                paused_for_approval=True,
                confirmation_call_id=confirmation_call_id,
                outcome=RemediationOutcome(
                    executed=False,
                    action_status="AWAITING_APPROVAL",
                    detail_message="高リスクまたは未知の事象のため自動変更を一時停止し、Slack 承認を待機しています。",
                ),
            )

        outcome = RemediationOutcome.model_validate(state["outcome"])
        return WorkflowRunResult(
            alert=alert,
            session_id=session.id,
            invocation_id=invocation_id,
            decision=final_decision,
            paused_for_approval=False,
            outcome=outcome,
        )

    async def resume_from_approval(
        self,
        *,
        alert: AlertEvent,
        decision: TriageDecision,
        session_id: str,
        invocation_id: str,
        confirmation_call_id: str,
        approved: bool,
        approver_id: str,
    ) -> RemediationOutcome:
        """Slack Interactive ボタンの承認/却下結果を受け取り、停止していた ADK ワークフローを再開する。"""
        fn_part = types.Part.from_function_response(
            name="adk_request_input",
            response={"confirmed": approved},
        )
        fn_part.function_response.id = confirmation_call_id
        resume_message = types.Content(role="user", parts=[fn_part])

        async for _ev in self._runner.run_async(
            user_id=self.USER_ID,
            session_id=session_id,
            invocation_id=invocation_id,
            new_message=resume_message,
        ):
            pass

        if not approved:
            demoted_id: str | None = None
            if (
                decision.matched_runbook_id != "NONE"
                and decision.similarity_score >= self._settings.auto_remediate_similarity_threshold
                and hasattr(self._kb, "demote_runbook_to_approval")
            ):
                demoted = await asyncio.to_thread(
                    self._kb.demote_runbook_to_approval,
                    decision.matched_runbook_id,
                )
                if demoted:
                    demoted_id = decision.matched_runbook_id
                    _emit_step_log(
                        "担当者による却下を受け、既存 Runbook を REQUIRE_APPROVAL へ自動降格しました。",
                        incident_id=alert.incident_id,
                        demoted_runbook_id=demoted_id,
                    )

            demote_suffix = (
                f"（Runbook {demoted_id} を REQUIRE_APPROVAL へ自動降格）"
                if demoted_id
                else ""
            )
            await asyncio.to_thread(
                self._guardrails.record_outcome,
                alert=alert,
                decision="REJECTED",
                remediation_type="NONE",
                matched_runbook_id=decision.matched_runbook_id,
                similarity_score=decision.similarity_score,
                verification_passed=False,
                summary=f"担当者 ({approver_id}) により自動処置案が却下され、手動調査へ移行しました。{demote_suffix}",
            )
            return RemediationOutcome(
                executed=False,
                action_status="REJECTED",
                detail_message=f"承認者 <@{approver_id}> が自動操作を却下し、手動対応へ切り替えました。{demote_suffix}",
                verification_passed=False,
                demoted_runbook_id=demoted_id,
            )

        updated_session = await self._sessions.get_session(
            app_name=self.APP_NAME,
            user_id=self.USER_ID,
            session_id=session_id,
        )
        state = updated_session.state if updated_session else {}
        outcome = RemediationOutcome.model_validate(state["outcome"])

        # 1. 初見・低類似度事象が承認実行後に正常回復した場合、構造化 Runbook を知識ベースへ自律登録する
        if (
            self._settings.enable_autonomous_kb_learning
            and outcome.verification_passed
            and decision.similarity_score < self._settings.auto_remediate_similarity_threshold
            and hasattr(self._kb, "upsert_learned_runbook")
        ):
            entry = await self._synthesizer(alert, decision, outcome)
            inserted = await asyncio.to_thread(self._kb.upsert_learned_runbook, entry)
            if inserted:
                outcome = outcome.model_copy(
                    update={
                        "learned_runbook_id": entry.runbook_id,
                        "learned_recommended_action": entry.recommended_action,
                        "detail_message": (
                            f"{outcome.detail_message} | "
                            f"新規 Runbook {entry.runbook_id} を知識ベースへ自律登録しました"
                            f"（次回推奨区分: {entry.recommended_action}）"
                        ),
                    }
                )
                _emit_step_log(
                    "承認解決済みの初見インシデントを知識ベースへ自律登録しました。",
                    incident_id=alert.incident_id,
                    learned_runbook_id=entry.runbook_id,
                    learned_recommended_action=entry.recommended_action,
                )
        # 2. 既存の高類似度 Runbook（鮮度期限切れ再評価を含む）が承認実行後に回復確認できた場合、last_verified_at を更新する
        elif (
            outcome.verification_passed
            and decision.matched_runbook_id != "NONE"
            and decision.similarity_score >= self._settings.auto_remediate_similarity_threshold
            and hasattr(self._kb, "refresh_runbook_verification")
        ):
            refreshed = await asyncio.to_thread(
                self._kb.refresh_runbook_verification,
                decision.matched_runbook_id,
            )
            if refreshed:
                outcome = outcome.model_copy(
                    update={
                        "refreshed_runbook_id": decision.matched_runbook_id,
                        "detail_message": (
                            f"{outcome.detail_message} | "
                            f"Runbook {decision.matched_runbook_id} の鮮度 (last_verified_at) を再承認更新しました"
                        ),
                    }
                )
                _emit_step_log(
                    "承認実行成功により既存 Runbook の最終検証日時を更新しました。",
                    incident_id=alert.incident_id,
                    refreshed_runbook_id=decision.matched_runbook_id,
                )
        # 3. 承認実行しても事後回復検証が NG だった場合、既存 AUTO_REMEDIATE Runbook を REQUIRE_APPROVAL へ降格する
        elif (
            not outcome.verification_passed
            and decision.matched_runbook_id != "NONE"
            and hasattr(self._kb, "demote_runbook_to_approval")
        ):
            demoted = await asyncio.to_thread(
                self._kb.demote_runbook_to_approval,
                decision.matched_runbook_id,
            )
            if demoted:
                outcome = outcome.model_copy(
                    update={
                        "demoted_runbook_id": decision.matched_runbook_id,
                        "detail_message": (
                            f"{outcome.detail_message} | "
                            f"事後検証 NG のため Runbook {decision.matched_runbook_id} を REQUIRE_APPROVAL へ自動降格しました"
                        ),
                    }
                )

        return outcome

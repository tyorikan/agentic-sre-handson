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
from .vector_kb import RunbookKnowledgeBase

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
            "1. 一過性エラーで既に自然減衰傾向（is_naturally_decaying=True）または過去事例が OBSERVE 推奨の場合:\n"
            "   -> decision='OBSERVE', remediation_type='NONE' とする（不要な再起動でコネクション再確立嵐を起こさないこと）。\n"
            "2. 直近デプロイ起因の既知バグや既知の Stateless リソース肥大化で、過去事例と強く一致する場合:\n"
            "   -> decision='AUTO_REMEDIATE' とし、remediation_type に 'ROLLBACK_DEPLOYMENT' または 'RESTART_DEPLOYMENT' を指定する。\n"
            "3. DB ロック競合・スキーマ不整合・未知の障害・確信度が低い場合:\n"
            "   -> decision='REQUIRE_APPROVAL' とし、勝手に本番変更せず人間の判断を仰ぐ。"
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
                    if part.text:
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
        session_service: BaseSessionService | None = None,
    ) -> None:
        self._settings = settings
        self._sandbox = sandbox_runner
        self._kb = knowledge_base
        self._guardrails = guardrail_manager
        self._remediation = remediation_service
        self._analyzer = decision_analyzer or build_llm_decision_analyzer(settings)
        self._enricher = telemetry_enricher
        self._sessions = session_service or build_session_service(settings)
        self._app = self._build_adk_app()
        self._runner = Runner(app=self._app, session_service=self._sessions)

    def _build_adk_app(self) -> App:
        sandbox = self._sandbox
        kb = self._kb
        analyzer = self._analyzer
        enricher = self._enricher
        guardrails = self._guardrails
        remediation = self._remediation

        @node(
            name="analyze_metrics_in_sandbox",
            timeout=75.0,
            retry_config=RetryConfig(max_attempts=2, initial_delay=0.5),
        )
        async def analyze_metrics_in_sandbox(ctx: Context) -> dict[str, Any]:
            alert = AlertEvent.model_validate(ctx.state["alert"])
            if enricher is not None:
                alert = await enricher(alert)
                ctx.state["alert"] = alert.model_dump()
            stats = sandbox.analyze_error_series(alert.error_rate_series)
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
            matches = kb.search_similar(alert, top_k=3)
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

            safe_decision, safe_rem, downgrade_reason = guardrails.enforce_safety_guardrails(
                namespace=alert.namespace,
                service_name=alert.service_name,
                proposed_decision=decision.decision,
                proposed_remediation=decision.remediation_type,
                similarity_score=decision.similarity_score,
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
                }
            )
            ctx.state["final_decision"] = final_decision.model_dump()
            _emit_step_log(
                "安全ガードレール検証・ルーティング判定を完了しました。",
                incident_id=alert.incident_id,
                final_route=final_decision.decision,
                remediation_type=final_decision.remediation_type,
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
            guardrails.record_outcome(
                alert=alert,
                decision="OBSERVE",
                remediation_type="NONE",
                matched_runbook_id=decision.matched_runbook_id,
                similarity_score=decision.similarity_score,
                verification_passed=True,
                summary=decision.rationale,
            )
            ctx.state["outcome"] = outcome.model_dump()
            return outcome.model_dump()

        @node(name="auto_remediate_node")
        async def auto_remediate_node(ctx: Context) -> dict[str, Any]:
            alert = AlertEvent.model_validate(ctx.state["alert"])
            decision = TriageDecision.model_validate(ctx.state["final_decision"])
            outcome = remediation.execute_and_verify(
                namespace=decision.target_namespace,
                deployment_name=decision.target_deployment,
                remediation_type=decision.remediation_type,
            )
            guardrails.record_outcome(
                alert=alert,
                decision="AUTO_REMEDIATE",
                remediation_type=decision.remediation_type,
                matched_runbook_id=decision.matched_runbook_id,
                similarity_score=decision.similarity_score,
                verification_passed=outcome.verification_passed,
                summary=f"{decision.rationale} | {outcome.detail_message}",
            )
            ctx.state["outcome"] = outcome.model_dump()
            return outcome.model_dump()

        @node(name="approved_remediate_node", require_confirmation=True)
        async def approved_remediate_node(ctx: Context) -> dict[str, Any]:
            """Slack の Interactive 承認ボタンで Approve が押された後にのみ本体が実行されるノード。"""
            alert = AlertEvent.model_validate(ctx.state["alert"])
            decision = TriageDecision.model_validate(ctx.state["final_decision"])

            if decision.remediation_type in ("ROLLBACK_DEPLOYMENT", "RESTART_DEPLOYMENT"):
                outcome = remediation.execute_and_verify(
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

            guardrails.record_outcome(
                alert=alert,
                decision="APPROVED_EXECUTED",
                remediation_type=decision.remediation_type,
                matched_runbook_id=decision.matched_runbook_id,
                similarity_score=decision.similarity_score,
                verification_passed=outcome.verification_passed,
                summary=f"[人間承認済み] {outcome.detail_message}",
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
        """新規アラートに対してワークフローを起動する（承認が必要な場合は一時停止状態で返る）。"""
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
            self._guardrails.record_outcome(
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
            self._guardrails.record_outcome(
                alert=alert,
                decision="REJECTED",
                remediation_type="NONE",
                matched_runbook_id=decision.matched_runbook_id,
                similarity_score=decision.similarity_score,
                verification_passed=False,
                summary=f"担当者 ({approver_id}) により自動処置案が却下され、手動調査へ移行しました。",
            )
            return RemediationOutcome(
                executed=False,
                action_status="REJECTED",
                detail_message=f"承認者 <@{approver_id}> が自動操作を却下し、手動対応へ切り替えました。",
                verification_passed=False,
            )

        updated_session = await self._sessions.get_session(
            app_name=self.APP_NAME,
            user_id=self.USER_ID,
            session_id=session_id,
        )
        state = updated_session.state if updated_session else {}
        return RemediationOutcome.model_validate(state["outcome"])

"""BigQuery 自律埋め込み生成（AI.EMBED / AI.SEARCH）対応の知識ベース検索・自律登録モジュール。

1. 検索 (`search_similar`):
   `incident_runbooks` テーブルの自動生成埋め込み列（`GENERATED ALWAYS AS (AI.EMBED(...))`）に対し、
   `AI.SEARCH` で類似インシデント上位 K 件と推奨対応手順（Runbook）を取得します。
2. 自律ナレッジ蓄積 (`upsert_learned_runbook`):
   初見障害が人間の Slack 承認を経て解決・回復確認された際、構造化された新規 Runbook を
   `MERGE` で冪等に追加します（ベクトルは BigQuery がバックグラウンドで自動生成・維持します）。
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from google.adk import Agent, Runner
from google.adk.apps import App
from google.adk.models.google_llm import Gemini
from google.adk.sessions import InMemorySessionService
from google.cloud import bigquery
from google.genai import types

from .config import AgentSettings
from .models import (
    AlertEvent,
    DecisionType,
    RemediationOutcome,
    SimilarRunbookMatch,
    SynthesizedRunbookDraft,
    SynthesizedRunbookEntry,
    TriageDecision,
)

RunbookSynthesizerFn = Callable[
    [AlertEvent, TriageDecision, RemediationOutcome],
    Awaitable[SynthesizedRunbookEntry],
]


class RunbookKnowledgeBase(Protocol):
    """本番 BigQuery AI.SEARCH とローカル検証用知識ベースの共通インターフェース。"""

    def search_similar(self, alert: AlertEvent, top_k: int = 3) -> list[SimilarRunbookMatch]:
        """アラート事象に類似する過去インシデント上位 top_k 件を類似度降順で返す。"""
        ...

    def upsert_learned_runbook(self, entry: SynthesizedRunbookEntry) -> bool:
        """承認解決済みの新規 Runbook を冪等に登録する（新規追加時 True、既存スキップ時 False）。"""
        ...


def build_symptom_query_text(alert: AlertEvent) -> str:
    """アラート情報から意味検索用のクエリ文字列を組み立てる。"""
    change_text = (
        "直近30分以内にDeploymentロールアウト変更あり"
        if alert.recent_deployment_within_30m
        else "直近の自社デプロイ変更なし"
    )
    return (
        f"サービス: {alert.service_name} (namespace={alert.namespace})\n"
        f"エラーコード: {alert.error_code} / 重大度: {alert.severity}\n"
        f"症状要約: {alert.summary}\n"
        f"変更相関: {change_text}"
    )


def determine_promoted_action(
    decision: TriageDecision,
    outcome: RemediationOutcome,
) -> DecisionType:
    """承認実行結果から次回発生時の推奨判断区分を決定論的に算出する。

    - Rollback / Restart を実際に実行して事後エラー率 < 1.0% の回復が確認された事象 -> AUTO_REMEDIATE へ昇格
    - 手動対応引き継ぎ（MANUAL_INTERVENTION）または未回復の事象 -> REQUIRE_APPROVAL を維持
    """
    if (
        outcome.executed
        and outcome.verification_passed
        and decision.remediation_type in ("ROLLBACK_DEPLOYMENT", "RESTART_DEPLOYMENT")
    ):
        return "AUTO_REMEDIATE"
    return "REQUIRE_APPROVAL"


def synthesize_runbook_deterministic(
    alert: AlertEvent,
    decision: TriageDecision,
    outcome: RemediationOutcome,
) -> SynthesizedRunbookEntry:
    """承認解決済みのインシデント情報から決定論的に構造化 Runbook エントリを組み立てる。"""
    promoted_action = determine_promoted_action(decision, outcome)
    runbook_id = f"AUTO-{alert.compute_fingerprint()[:8].upper()}"
    change_hint = (
        "直近30分以内に Deployment ロールアウト変更あり"
        if alert.recent_deployment_within_30m
        else "直近の自社デプロイ変更なし（稼働中プロセスの状態劣化または外部要因）"
    )
    if decision.remediation_type == "ROLLBACK_DEPLOYMENT":
        steps = f"Deployment {alert.namespace}/{alert.service_name} を直前の安定リビジョンへロールバックし、エラー率 < 1.0% を確認する。"
        category = "GKE デプロイ・設定起因バグ"
    elif decision.remediation_type == "RESTART_DEPLOYMENT":
        steps = f"Deployment {alert.namespace}/{alert.service_name} をローリング再起動して破損したインメモリ状態を初期化し、エラー率 < 1.0% を確認する。"
        category = "メモリリーク・FD/PID 枯渇"
    else:
        steps = f"Pod の無断再起動を避け、オンコール担当者が {alert.service_name} の依存リソース・ロック状態を調査して解消する。"
        category = "DB 接続枯渇・ロック競合"

    return SynthesizedRunbookEntry(
        runbook_id=runbook_id,
        category=category,
        title=f"[自律学習] {alert.service_name} における {alert.error_code} 障害への対応",
        service_pattern=f"GKE Deployment ({alert.namespace}/{alert.service_name})",
        symptom_signature=f"{alert.error_code}: {alert.summary}",
        metrics_pattern="エラー率が継続的に上昇し自然減衰しない持続的エラーパターン",
        recent_change_hint=change_hint,
        root_cause=decision.root_cause_hypothesis,
        recommended_action=promoted_action,
        remediation_type=decision.remediation_type,
        remediation_steps=steps,
        anti_patterns=(
            decision.anti_pattern_warning
            or "事後メトリクス回復確認を行わずに連続して再起動やロールバックを繰り返さないこと。"
        ),
        verification_check="処置完了後のプローブ計測で HTTP 5xx エラー率が 1.0% 未満に低下していること。",
        historical_mttr_minutes=5,
    )


def build_llm_runbook_synthesizer(settings: AgentSettings) -> RunbookSynthesizerFn:
    """Gemini 3.8 Flash を用いて解決済みインシデントから高品質な構造化 Runbook を生成する関数を返す。"""
    synth_agent = Agent(
        name="sre_runbook_synthesizer",
        model=Gemini(
            model=settings.triage_model,
            retry_options=types.HttpRetryOptions(attempts=2),
        ),
        instruction=(
            "あなたは本番 SRE チームのナレッジベース管理者です。\n"
            "人間の承認を経て解決・確認された初見インシデントの情報を受け取り、\n"
            "次回同様の事象が発生した際に AI.SEARCH で高精度にヒットし、安全な判断根拠となるよう\n"
            "簡潔・正確な日本語で SynthesizedRunbookDraft スキーマを出力してください。\n"
            "推測や未確認の事項は書かず、与えられたエラー兆候・根本原因仮説・実証された処置内容に基づくこと。"
        ),
        output_schema=SynthesizedRunbookDraft,
    )
    app = App(name="sre_runbook_synth_app", root_agent=synth_agent)

    async def _synthesize(
        alert: AlertEvent,
        decision: TriageDecision,
        outcome: RemediationOutcome,
    ) -> SynthesizedRunbookEntry:
        fallback = synthesize_runbook_deterministic(alert, decision, outcome)
        try:
            mem_sessions = InMemorySessionService()
            runner = Runner(app=app, session_service=mem_sessions)
            sess = await mem_sessions.create_session(app_name="sre_runbook_synth_app", user_id="sre_kb_curator")
            prompt = json.dumps(
                {
                    "alert": alert.model_dump(),
                    "triage_decision": decision.model_dump(),
                    "remediation_outcome": outcome.model_dump(),
                },
                ensure_ascii=False,
                indent=2,
            )
            last_text = ""
            async for ev in runner.run_async(
                user_id="sre_kb_curator",
                session_id=sess.id,
                new_message=types.Content(role="user", parts=[types.Part.from_text(text=prompt)]),
            ):
                if ev.content and ev.content.parts:
                    for part in ev.content.parts:
                        if part.text and not getattr(part, "thought", False):
                            last_text = part.text
            if not last_text:
                return fallback
            draft = SynthesizedRunbookDraft.model_validate_json(last_text)
            return SynthesizedRunbookEntry(
                runbook_id=fallback.runbook_id,
                category=draft.category,
                title=draft.title,
                service_pattern=draft.service_pattern,
                symptom_signature=fallback.symptom_signature,
                metrics_pattern=draft.metrics_pattern,
                recent_change_hint=draft.recent_change_hint,
                root_cause=draft.root_cause,
                # 次回推奨アクションと復旧種別はコード側の安全ポリシーで決定論的に強制する
                recommended_action=fallback.recommended_action,
                remediation_type=fallback.remediation_type,
                remediation_steps=draft.remediation_steps,
                anti_patterns=draft.anti_patterns,
                verification_check=draft.verification_check,
                historical_mttr_minutes=5,
            )
        except Exception:  # noqa: BLE001
            return fallback

    return _synthesize


class BigQueryVectorKnowledgeBase:
    """BigQuery 自律埋め込み生成列 (`AI.EMBED`) と `AI.SEARCH` を用いた本番実装。"""

    def __init__(self, settings: AgentSettings, client: bigquery.Client | None = None) -> None:
        self._settings = settings
        self._client = client or bigquery.Client(project=settings.project_id, location=settings.region)
        self._table_fqn = f"{settings.project_id}.{settings.bq_dataset}.{settings.bq_runbooks_table}"

    def search_similar(self, alert: AlertEvent, top_k: int = 3) -> list[SimilarRunbookMatch]:
        query_text = build_symptom_query_text(alert)
        sql = f"""
            SELECT
              base.runbook_id,
              base.category,
              base.title,
              base.root_cause,
              base.recommended_action,
              base.remediation_type,
              base.remediation_steps,
              base.anti_patterns,
              base.verification_check,
              GREATEST(0.0, LEAST(1.0, 1.0 - distance)) AS similarity_score
            FROM AI.SEARCH(
              TABLE `{self._table_fqn}`,
              'search_document',
              @query_text,
              distance_type => 'COSINE',
              top_k => @top_k
            )
            ORDER BY similarity_score DESC
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("query_text", "STRING", query_text),
                bigquery.ScalarQueryParameter("top_k", "INT64", top_k),
            ]
        )
        rows = self._client.query(sql, job_config=job_config).result()
        return [
            SimilarRunbookMatch(
                runbook_id=row.runbook_id,
                category=row.category,
                title=row.title,
                root_cause=row.root_cause,
                recommended_action=row.recommended_action,
                remediation_type=row.remediation_type,
                remediation_steps=row.remediation_steps,
                anti_patterns=row.anti_patterns,
                verification_check=row.verification_check,
                similarity_score=round(float(row.similarity_score), 4),
            )
            for row in rows
        ]

    def upsert_learned_runbook(self, entry: SynthesizedRunbookEntry) -> bool:
        """新規 Runbook を MERGE で冪等に追加する（embedding 列は BigQuery AI.EMBED が自動生成）。"""
        search_doc = entry.build_search_document()
        sql = f"""
            MERGE `{self._table_fqn}` AS target
            USING (SELECT @runbook_id AS runbook_id) AS src
            ON target.runbook_id = src.runbook_id
            WHEN NOT MATCHED THEN
              INSERT (
                runbook_id,
                category,
                title,
                service_pattern,
                symptom_signature,
                metrics_pattern,
                recent_change_hint,
                root_cause,
                recommended_action,
                remediation_type,
                remediation_steps,
                anti_patterns,
                verification_check,
                historical_mttr_minutes,
                search_document
              )
              VALUES (
                @runbook_id,
                @category,
                @title,
                @service_pattern,
                @symptom_signature,
                @metrics_pattern,
                @recent_change_hint,
                @root_cause,
                @recommended_action,
                @remediation_type,
                @remediation_steps,
                @anti_patterns,
                @verification_check,
                @historical_mttr_minutes,
                @search_document
              )
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("runbook_id", "STRING", entry.runbook_id),
                bigquery.ScalarQueryParameter("category", "STRING", entry.category),
                bigquery.ScalarQueryParameter("title", "STRING", entry.title),
                bigquery.ScalarQueryParameter("service_pattern", "STRING", entry.service_pattern),
                bigquery.ScalarQueryParameter("symptom_signature", "STRING", entry.symptom_signature),
                bigquery.ScalarQueryParameter("metrics_pattern", "STRING", entry.metrics_pattern),
                bigquery.ScalarQueryParameter("recent_change_hint", "STRING", entry.recent_change_hint),
                bigquery.ScalarQueryParameter("root_cause", "STRING", entry.root_cause),
                bigquery.ScalarQueryParameter("recommended_action", "STRING", entry.recommended_action),
                bigquery.ScalarQueryParameter("remediation_type", "STRING", entry.remediation_type),
                bigquery.ScalarQueryParameter("remediation_steps", "STRING", entry.remediation_steps),
                bigquery.ScalarQueryParameter("anti_patterns", "STRING", entry.anti_patterns),
                bigquery.ScalarQueryParameter("verification_check", "STRING", entry.verification_check),
                bigquery.ScalarQueryParameter("historical_mttr_minutes", "INT64", entry.historical_mttr_minutes),
                bigquery.ScalarQueryParameter("search_document", "STRING", search_doc),
            ]
        )
        job = self._client.query(sql, job_config=job_config)
        job.result()
        return bool(job.num_dml_affected_rows and job.num_dml_affected_rows > 0)


class LocalJsonlKnowledgeBase:
    """ローカル開発・単体テスト向けに `seed_runbooks_100.jsonl` を読み込み、自律追記にも対応する実装。"""

    def __init__(self, jsonl_path: Path) -> None:
        self._lock = threading.Lock()
        self._records: list[dict] = [
            json.loads(line)
            for line in jsonl_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @property
    def total_records(self) -> int:
        with self._lock:
            return len(self._records)

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        tokens = set(re.findall(r"[A-Za-z0-9_]{2,}|[\u3040-\u30ff\u4e00-\u9fff]{2,}", text.lower()))
        return tokens

    def search_similar(self, alert: AlertEvent, top_k: int = 3) -> list[SimilarRunbookMatch]:
        q_tokens = self._tokenize(build_symptom_query_text(alert))
        with self._lock:
            snapshot = list(self._records)

        scored: list[tuple[float, dict]] = []
        for rec in snapshot:
            doc_text = " ".join(
                [
                    rec["title"],
                    rec["symptom_signature"],
                    rec["metrics_pattern"],
                    rec["recent_change_hint"],
                    rec["root_cause"],
                ]
            )
            d_tokens = self._tokenize(doc_text)
            overlap = len(q_tokens & d_tokens)
            base_score = overlap / max(1, min(len(q_tokens), len(d_tokens)))
            if alert.error_code.lower() in rec["symptom_signature"].lower():
                base_score = max(base_score, 0.92)
            scored.append((min(0.99, round(base_score, 4)), rec))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            SimilarRunbookMatch(
                runbook_id=rec["runbook_id"],
                category=rec["category"],
                title=rec["title"],
                root_cause=rec["root_cause"],
                recommended_action=rec["recommended_action"],
                remediation_type=rec["remediation_type"],
                remediation_steps=rec["remediation_steps"],
                anti_patterns=rec["anti_patterns"],
                verification_check=rec["verification_check"],
                similarity_score=score,
            )
            for score, rec in scored[:top_k]
        ]

    def upsert_learned_runbook(self, entry: SynthesizedRunbookEntry) -> bool:
        with self._lock:
            if any(r["runbook_id"] == entry.runbook_id for r in self._records):
                return False
            self._records.append(entry.model_dump())
            return True

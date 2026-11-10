"""BigQuery ベクトル知識ベース検索モジュール。

現在のアラート症状・メトリクス傾向・直近変更有無を結合したクエリ文を
BigQuery の `VECTOR_SEARCH` + `ML.GENERATE_EMBEDDING`（`gemini-embedding-2`）で照合し、
過去の類似インシデント上位 K 件と推奨対応手順（Runbook）を返します。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol

from google.cloud import bigquery

from .config import AgentSettings
from .models import AlertEvent, SimilarRunbookMatch


class RunbookKnowledgeBase(Protocol):
    """本番 BigQuery ベクトル検索とローカル検証用知識ベースの共通インターフェース。"""

    def search_similar(self, alert: AlertEvent, top_k: int = 3) -> list[SimilarRunbookMatch]:
        """アラート事象に類似する過去インシデント上位 top_k 件を類似度降順で返す。"""
        ...


def build_symptom_query_text(alert: AlertEvent) -> str:
    """アラート情報からベクトル検索用のクエリ文字列を組み立てる。"""
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


class BigQueryVectorKnowledgeBase:
    """BigQuery `VECTOR_SEARCH` と `gemini-embedding-2` リモートモデルを用いた本番実装。"""

    def __init__(self, settings: AgentSettings, client: bigquery.Client | None = None) -> None:
        self._settings = settings
        self._client = client or bigquery.Client(project=settings.project_id, location=settings.region)
        self._table_fqn = f"{settings.project_id}.{settings.bq_dataset}.{settings.bq_runbooks_table}"
        self._model_fqn = f"{settings.project_id}.{settings.bq_dataset}.embedding_model"

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
            FROM VECTOR_SEARCH(
              TABLE `{self._table_fqn}`,
              'embedding',
              (
                SELECT ml_generate_embedding_result AS embedding
                FROM ML.GENERATE_EMBEDDING(
                  MODEL `{self._model_fqn}`,
                  (SELECT @query_text AS content),
                  STRUCT(TRUE AS flatten_json_output)
                )
              ),
              top_k => @top_k,
              distance_type => 'COSINE'
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


class LocalJsonlKnowledgeBase:
    """ローカル開発・単体テスト向けに `seed_runbooks_100.jsonl` を直接トークン類似度検索する実装。"""

    def __init__(self, jsonl_path: Path) -> None:
        self._records = [
            json.loads(line)
            for line in jsonl_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        tokens = set(re.findall(r"[A-Za-z0-9_]{2,}|[\u3040-\u30ff\u4e00-\u9fff]{2,}", text.lower()))
        return tokens

    def search_similar(self, alert: AlertEvent, top_k: int = 3) -> list[SimilarRunbookMatch]:
        q_tokens = self._tokenize(build_symptom_query_text(alert))
        scored: list[tuple[float, dict]] = []
        for rec in self._records:
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
            # 代表的なエラーコード文字列が直接一致している場合は高確信度ブースト
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

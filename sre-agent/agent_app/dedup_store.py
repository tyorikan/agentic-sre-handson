"""アラート重複排除・クールダウン判定・サーキットブレーカー・監査記録モジュール。

本番運用で頻発する「同一障害で 5 分間に数十通のアラートが発火し、LLM 呼び出しと
再起動コマンドが連打される事故」を決定論的ロジックで防ぎます。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from google.cloud import bigquery

from .config import AgentSettings
from .models import AlertEvent, DecisionType, RemediationType


@dataclass(frozen=True)
class IncidentEventRecord:
    """BigQuery `incident_events` テーブルの 1 行に対応する不変レコード。"""

    event_id: str
    incident_id: str
    fingerprint: str
    namespace: str
    service_name: str
    error_code: str
    decision: str
    remediation_type: str
    matched_runbook_id: str
    similarity_score: float
    verification_passed: bool
    summary: str
    created_at: datetime


class EventStoreBackend(Protocol):
    """BigQuery 本番実装とインメモリ単体テスト実装を切り替えるためのプロトコル。"""

    def find_recent_by_fingerprint(self, fingerprint: str, since: datetime) -> list[IncidentEventRecord]:
        """指定時刻以降の同一 fingerprint イベント一覧を返す。"""
        ...

    def count_auto_actions_for_service(self, namespace: str, service_name: str, since: datetime) -> int:
        """指定時刻以降に対象サービスへ自動実行された復旧操作の件数を返す。"""
        ...

    def append_event(self, record: IncidentEventRecord) -> None:
        """監査・状態イベントを追記する。"""
        ...


class InMemoryEventStoreBackend:
    """ローカル開発・自動テスト用のインメモリ追記型ストア。"""

    def __init__(self) -> None:
        self._rows: list[IncidentEventRecord] = []

    @property
    def rows(self) -> list[IncidentEventRecord]:
        return list(self._rows)

    def find_recent_by_fingerprint(self, fingerprint: str, since: datetime) -> list[IncidentEventRecord]:
        return [r for r in self._rows if r.fingerprint == fingerprint and r.created_at >= since]

    def count_auto_actions_for_service(self, namespace: str, service_name: str, since: datetime) -> int:
        return sum(
            1
            for r in self._rows
            if r.namespace == namespace
            and r.service_name == service_name
            and r.decision == "AUTO_REMEDIATE"
            and r.created_at >= since
        )

    def append_event(self, record: IncidentEventRecord) -> None:
        self._rows.append(record)


class BigQueryEventStoreBackend:
    """BigQuery `incident_events` テーブルを用いる本番用追記型ストア。"""

    def __init__(self, settings: AgentSettings, client: bigquery.Client | None = None) -> None:
        self._settings = settings
        self._client = client or bigquery.Client(project=settings.project_id, location=settings.region)
        self._table_fqn = f"{settings.project_id}.{settings.bq_dataset}.{settings.bq_events_table}"

    def find_recent_by_fingerprint(self, fingerprint: str, since: datetime) -> list[IncidentEventRecord]:
        sql = f"""
            SELECT event_id, incident_id, fingerprint, namespace, service_name, error_code,
                   decision, remediation_type, matched_runbook_id, similarity_score,
                   verification_passed, summary, created_at
            FROM `{self._table_fqn}`
            WHERE fingerprint = @fingerprint
              AND created_at >= @since
            ORDER BY created_at DESC
            LIMIT 5
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("fingerprint", "STRING", fingerprint),
                bigquery.ScalarQueryParameter("since", "TIMESTAMP", since),
            ]
        )
        rows = self._client.query(sql, job_config=job_config).result()
        return [
            IncidentEventRecord(
                event_id=row.event_id,
                incident_id=row.incident_id,
                fingerprint=row.fingerprint,
                namespace=row.namespace,
                service_name=row.service_name,
                error_code=row.error_code,
                decision=row.decision,
                remediation_type=row.remediation_type,
                matched_runbook_id=row.matched_runbook_id or "NONE",
                similarity_score=float(row.similarity_score or 0.0),
                verification_passed=bool(row.verification_passed),
                summary=row.summary or "",
                created_at=row.created_at,
            )
            for row in rows
        ]

    def count_auto_actions_for_service(self, namespace: str, service_name: str, since: datetime) -> int:
        sql = f"""
            SELECT COUNT(*) AS cnt
            FROM `{self._table_fqn}`
            WHERE namespace = @namespace
              AND service_name = @service_name
              AND decision = 'AUTO_REMEDIATE'
              AND created_at >= @since
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("namespace", "STRING", namespace),
                bigquery.ScalarQueryParameter("service_name", "STRING", service_name),
                bigquery.ScalarQueryParameter("since", "TIMESTAMP", since),
            ]
        )
        row = next(iter(self._client.query(sql, job_config=job_config).result()))
        return int(row.cnt)

    def append_event(self, record: IncidentEventRecord) -> None:
        errors = self._client.insert_rows_json(
            self._table_fqn,
            [
                {
                    "event_id": record.event_id,
                    "incident_id": record.incident_id,
                    "fingerprint": record.fingerprint,
                    "namespace": record.namespace,
                    "service_name": record.service_name,
                    "error_code": record.error_code,
                    "decision": record.decision,
                    "remediation_type": record.remediation_type,
                    "matched_runbook_id": record.matched_runbook_id,
                    "similarity_score": record.similarity_score,
                    "verification_passed": record.verification_passed,
                    "summary": record.summary,
                    "created_at": record.created_at.isoformat(),
                }
            ],
        )
        if errors:
            raise RuntimeError(f"BigQuery 監査イベントの書き込みに失敗しました: {errors}")


class IncidentGuardrailManager:
    """重複排除・クールダウン・自動実行回数上限（サーキットブレーカー）を一元管理する。"""

    def __init__(self, settings: AgentSettings, backend: EventStoreBackend) -> None:
        self._settings = settings
        self._backend = backend

    def is_in_cooldown(self, alert: AlertEvent, now: datetime | None = None) -> tuple[bool, str]:
        """直近のクールダウン期間内に同一障害が処理済みであれば True と理由を返す。"""
        current = now or datetime.now(timezone.utc)
        since = current - timedelta(minutes=self._settings.cooldown_minutes)
        fp = alert.compute_fingerprint()
        recent = self._backend.find_recent_by_fingerprint(fp, since=since)
        if recent:
            latest = recent[0]
            return (
                True,
                f"クールダウン期間内 ({self._settings.cooldown_minutes}分) に同一 fingerprint={fp} が処理済みです "
                f"(前回 incident_id={latest.incident_id}, decision={latest.decision})",
            )
        return False, ""

    def enforce_safety_guardrails(
        self,
        *,
        namespace: str,
        service_name: str,
        proposed_decision: DecisionType,
        proposed_remediation: RemediationType,
        similarity_score: float,
        now: datetime | None = None,
    ) -> tuple[DecisionType, RemediationType, str]:
        """LLM が提案した判断に対し、決定論的ルールで安全側への降格（ダウングレード）を強制する。"""
        if proposed_decision == "OBSERVE":
            return "OBSERVE", "NONE", ""

        # 1. 許可されていない Namespace は自動変更を絶対禁止
        if namespace not in self._settings.allowed_namespaces:
            return (
                "REQUIRE_APPROVAL",
                proposed_remediation,
                f"対象 Namespace '{namespace}' は自動復旧許可リスト {self._settings.allowed_namespaces} 外のため人間承認へ降格しました。",
            )

        if proposed_decision == "AUTO_REMEDIATE":
            # 2. 過去インシデント類似度がしきい値未満なら自動復旧を禁止し承認待ちへ降格
            if similarity_score < self._settings.auto_remediate_similarity_threshold:
                return (
                    "REQUIRE_APPROVAL",
                    proposed_remediation,
                    f"類似度スコア ({similarity_score:.2f}) が自動復旧しきい値 "
                    f"({self._settings.auto_remediate_similarity_threshold:.2f}) 未満のため人間承認へ降格しました。",
                )

            # 3. MANUAL_INTERVENTION 種別は自動実行不可
            if proposed_remediation not in ("ROLLBACK_DEPLOYMENT", "RESTART_DEPLOYMENT"):
                return (
                    "REQUIRE_APPROVAL",
                    proposed_remediation,
                    f"復旧種別 '{proposed_remediation}' は自動実行対象外のため人間承認へ降格しました。",
                )

            # 4. サーキットブレーカー: 同一サービスへの自動復旧が直近 1 時間に上限回数に達していたら停止
            current = now or datetime.now(timezone.utc)
            one_hour_ago = current - timedelta(hours=1)
            auto_count = self._backend.count_auto_actions_for_service(namespace, service_name, since=one_hour_ago)
            if auto_count >= self._settings.max_auto_actions_per_hour:
                return (
                    "REQUIRE_APPROVAL",
                    proposed_remediation,
                    f"直近 1 時間の自動復旧回数が上限 ({auto_count}/{self._settings.max_auto_actions_per_hour} 回) "
                    "に到達したため、再起動ループ防止のため人間承認へ降格しました。",
                )

        return proposed_decision, proposed_remediation, ""

    def record_outcome(
        self,
        *,
        alert: AlertEvent,
        decision: str,
        remediation_type: str,
        matched_runbook_id: str,
        similarity_score: float,
        verification_passed: bool,
        summary: str,
        now: datetime | None = None,
    ) -> IncidentEventRecord:
        """最終判断と処置結果を追記テーブルへ記録する。"""
        record = IncidentEventRecord(
            event_id=uuid.uuid4().hex,
            incident_id=alert.incident_id,
            fingerprint=alert.compute_fingerprint(),
            namespace=alert.namespace,
            service_name=alert.service_name,
            error_code=alert.error_code,
            decision=decision,
            remediation_type=remediation_type,
            matched_runbook_id=matched_runbook_id,
            similarity_score=similarity_score,
            verification_passed=verification_passed,
            summary=summary,
            created_at=now or datetime.now(timezone.utc),
        )
        self._backend.append_event(record)
        return record

"""本番テレメトリ補完モジュール (`telemetry_enrichment.py`)。

Cloud Logging シンク経由で受信した生ログ通知には時系列メトリクス配列や
直近デプロイ履歴が含まれないため、Cloud Logging (`logging_v2`) と
Kubernetes ReplicaSet 履歴から実データを取得して `AlertEvent` を補完します。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Iterable, Protocol

from google.cloud import logging_v2

from .config import AgentSettings
from .models import AlertEvent

ERROR_SEVERITIES = {"ERROR", "CRITICAL", "ALERT", "EMERGENCY"}


def compute_error_rate_series_from_logs(
    entries: Iterable[tuple[datetime, str]],
    *,
    window_end: datetime,
    bucket_seconds: int = 18,
    num_buckets: int = 5,
) -> list[float]:
    """直近の (timestamp, severity) ログ列を時間バケットごとのエラー率配列へ変換する。"""
    if num_buckets <= 0 or bucket_seconds <= 0:
        return []

    total_span = timedelta(seconds=bucket_seconds * num_buckets)
    window_start = window_end - total_span
    totals = [0] * num_buckets
    errors = [0] * num_buckets

    for ts, severity in entries:
        ts_utc = ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        offset = (ts_utc - window_start).total_seconds()
        if offset < 0 or offset > total_span.total_seconds():
            continue
        idx = min(num_buckets - 1, int(offset // bucket_seconds))
        totals[idx] += 1
        if severity.upper() in ERROR_SEVERITIES:
            errors[idx] += 1

    # 無通信バケット (total == 0) は「エラー率 0% の正常通信」とは異なるため除外し、
    # 実トラフィックが発生したバケットのエラー率推移のみを返す
    rates: list[float] = []
    for total, err in zip(totals, errors):
        if total == 0:
            continue
        rates.append(round(err / total, 4))
    return rates


class DeploymentHistoryChecker(Protocol):
    """直近デプロイ有無を判定するためのプロトコル。"""

    def has_recent_deployment(self, namespace: str, deployment_name: str, *, within_minutes: int = 30) -> bool:
        ...


class CloudTelemetryEnricher:
    """Cloud Logging の実エラー率時系列と GKE ReplicaSet 更新履歴を補完するサービス。"""

    def __init__(
        self,
        settings: AgentSettings,
        deployment_checker: DeploymentHistoryChecker | None = None,
    ) -> None:
        self._settings = settings
        self._deployment_checker = deployment_checker

    def _fetch_recent_log_entries(
        self,
        namespace: str,
        service_name: str,
        lookback_seconds: int = 90,
    ) -> list[tuple[datetime, str]]:
        client = logging_v2.Client(project=self._settings.project_id)
        since = (datetime.now(timezone.utc) - timedelta(seconds=lookback_seconds)).isoformat()
        log_filter = (
            'resource.type="k8s_container" '
            f'AND resource.labels.namespace_name="{namespace}" '
            f'AND resource.labels.container_name="{service_name}" '
            'AND NOT textPayload=~"^INFO:" '
            f'AND timestamp>="{since}"'
        )
        items: list[tuple[datetime, str]] = []
        for entry in client.list_entries(
            filter_=log_filter,
            order_by=logging_v2.ASCENDING,
            page_size=200,
            max_results=300,
        ):
            if entry.timestamp is not None:
                items.append((entry.timestamp, str(entry.severity or "INFO").upper()))
        return items

    async def enrich_alert(self, alert: AlertEvent) -> AlertEvent:
        """不足している時系列エラー率と直近デプロイ有無を Google Cloud から補完する。"""
        updates: dict[str, object] = {}

        if not alert.error_rate_series:
            if self._settings.stabilization_wait_seconds > 0:
                await asyncio.sleep(self._settings.stabilization_wait_seconds)
            entries = await asyncio.to_thread(
                self._fetch_recent_log_entries,
                alert.namespace,
                alert.service_name,
                90,
            )
            series = compute_error_rate_series_from_logs(
                entries,
                window_end=datetime.now(timezone.utc),
                bucket_seconds=18,
                num_buckets=5,
            )
            if series:
                updates["error_rate_series"] = series

        if not alert.recent_deployment_within_30m and self._deployment_checker is not None:
            recent = await asyncio.to_thread(
                self._deployment_checker.has_recent_deployment,
                alert.namespace,
                alert.service_name,
                within_minutes=30,
            )
            if recent:
                updates["recent_deployment_within_30m"] = True

        return alert.model_copy(update=updates) if updates else alert

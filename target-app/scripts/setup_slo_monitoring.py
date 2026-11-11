#!/usr/bin/env python3
"""Cloud Logging カウンタメトリクスと Cloud Monitoring Service Monitoring SLO の冪等セットアップスクリプト。

作成・更新するリソース:
  1. Cloud Logging 単一アトミック・カウンタメトリクス:
     - order_requests_count : POST /orders の全注文ログを単一メトリクスで集計し、
                              ラベル `outcome = EXTRACT(severity)` (INFO=正常201 / ERROR,CRITICAL=異常5xx) で分類。
                              単一メトリクスに統一することで 2 メトリクス間の非同期フラッシュずれによる誤判定を防止します。
  2. Cloud Monitoring Service:
     - order-service-slo    : GKE Deployment (sre-demo-app/order-service) に対応する監視対象サービス
  3. Cloud Monitoring ServiceLevelObjective (1分窓 Windows-based 可用性 SLO × 2本):
     - order-availability-daily-99    : カレンダー日次 99.0% (許容エラーバジェット 14.4 分/日 — 訓練前後ゲージ確認用)
     - order-availability-monthly-999 : カレンダー月次 99.9% (許容エラーバジェット 43.2 分/月 — 本番インパクト換算用)

使い方:
  # セットアップ（作成または最新定義へ更新）
  python3 ./target-app/scripts/setup_slo_monitoring.py

  # クリーンアップ（第 6 章の削除手順で使用）
  python3 ./target-app/scripts/setup_slo_monitoring.py --delete
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from google.api import label_pb2, metric_pb2
from google.api_core import exceptions as gcp_exceptions
from google.cloud import monitoring_v3
from google.cloud.logging_v2.services.metrics_service_v2 import MetricsServiceV2Client
from google.cloud.logging_v2.types import logging_metrics
from google.protobuf import duration_pb2
from google.type import calendar_period_pb2

ORDER_METRIC_ID = "order_requests_count"
LEGACY_METRIC_IDS = ("order_requests_total", "order_requests_good")

ORDER_LOG_FILTER = (
    'resource.type="k8s_container"\n'
    'AND resource.labels.namespace_name="sre-demo-app"\n'
    'AND resource.labels.container_name="order-service"\n'
    'AND jsonPayload.service="order-service"\n'
    "AND (jsonPayload.order_id:* OR jsonPayload.error_code:*)"
)


def upsert_atomic_order_log_metric(client: MetricsServiceV2Client, project_id: str) -> None:
    """outcome ラベル付きの単一カウンタメトリクスを作成または更新する。"""
    parent = f"projects/{project_id}"
    metric_resource_name = f"projects/{project_id}/metrics/{ORDER_METRIC_ID}"
    descriptor = metric_pb2.MetricDescriptor(
        metric_kind=metric_pb2.MetricDescriptor.MetricKind.DELTA,
        value_type=metric_pb2.MetricDescriptor.ValueType.INT64,
        unit="1",
        labels=[
            label_pb2.LabelDescriptor(
                key="outcome",
                value_type=label_pb2.LabelDescriptor.ValueType.STRING,
                description="POST /orders 応答結果のログ重大度 (INFO=201正常, ERROR/CRITICAL=5xx異常)",
            )
        ],
    )
    metric = logging_metrics.LogMetric(
        name=ORDER_METRIC_ID,
        description="order-service POST /orders の注文リクエスト数 (outcome ラベルで正常・異常をアトミック分類)",
        filter=ORDER_LOG_FILTER,
        metric_descriptor=descriptor,
        label_extractors={"outcome": "EXTRACT(severity)"},
    )
    try:
        client.get_log_metric(metric_name=metric_resource_name)
        client.update_log_metric(metric_name=metric_resource_name, metric=metric)
        print(f"[OK] Cloud Logging カウンタメトリクスを更新しました: {ORDER_METRIC_ID}")
    except gcp_exceptions.NotFound:
        client.create_log_metric(parent=parent, metric=metric)
        print(f"[OK] Cloud Logging カウンタメトリクスを作成しました: {ORDER_METRIC_ID}")


def upsert_monitoring_service(
    client: monitoring_v3.ServiceMonitoringServiceClient,
    *,
    project_id: str,
    service_id: str,
    cluster_location: str,
    cluster_name: str,
    namespace: str = "sre-demo-app",
    deployment_name: str = "order-service",
) -> str:
    """Service Monitoring の対象 Service を作成または確認し、完全リソース名を返す。"""
    parent = f"projects/{project_id}"
    service_name = f"{parent}/services/{service_id}"

    try:
        existing = client.get_service(name=service_name)
        print(f"[OK] Service Monitoring サービスを確認しました: {existing.name}")
        return existing.name
    except gcp_exceptions.NotFound:
        pass

    gke_service = monitoring_v3.Service(
        display_name=f"Order Service ({namespace}/{deployment_name})",
        gke_workload=monitoring_v3.Service.GkeWorkload(
            location=cluster_location,
            cluster_name=cluster_name,
            namespace_name=namespace,
            top_level_controller_type="Deployment",
            top_level_controller_name=deployment_name,
        ),
        user_labels={"managed_by": "agentic-sre-handson", "app": "order-service"},
    )
    try:
        created = client.create_service(
            request=monitoring_v3.CreateServiceRequest(
                parent=parent,
                service_id=service_id,
                service=gke_service,
            )
        )
        print(f"[OK] Service Monitoring サービス (GKE Workload) を作成しました: {created.name}")
        return created.name
    except gcp_exceptions.InvalidArgument:
        custom_service = monitoring_v3.Service(
            display_name=f"Order Service ({namespace}/{deployment_name})",
            custom=monitoring_v3.Service.Custom(),
            user_labels={
                "managed_by": "agentic-sre-handson",
                "cluster": cluster_name,
                "namespace": namespace,
                "app": deployment_name,
            },
        )
        created = client.create_service(
            request=monitoring_v3.CreateServiceRequest(
                parent=parent,
                service_id=service_id,
                service=custom_service,
            )
        )
        print(f"[OK] Service Monitoring サービス (Custom) を作成しました: {created.name}")
        return created.name


def build_windows_availability_sli(namespace: str = "sre-demo-app") -> monitoring_v3.ServiceLevelIndicator:
    """単一メトリクス order_requests_count の outcome=INFO 比率が 95% 以上の分を正常分とする SLI を構築する。"""
    base_filter = (
        f'metric.type="logging.googleapis.com/user/{ORDER_METRIC_ID}" '
        'AND resource.type="k8s_container" '
        f'AND resource.labels.namespace_name="{namespace}"'
    )
    good_filter = f'{base_filter} AND metric.labels.outcome="INFO"'
    return monitoring_v3.ServiceLevelIndicator(
        windows_based=monitoring_v3.WindowsBasedSli(
            window_period=duration_pb2.Duration(seconds=60),
            good_total_ratio_threshold=monitoring_v3.WindowsBasedSli.PerformanceThreshold(
                threshold=0.95,
                performance=monitoring_v3.RequestBasedSli(
                    good_total_ratio=monitoring_v3.TimeSeriesRatio(
                        good_service_filter=good_filter,
                        total_service_filter=base_filter,
                    )
                ),
            ),
        )
    )


def upsert_slo(
    client: monitoring_v3.ServiceMonitoringServiceClient,
    *,
    service_resource_name: str,
    slo_id: str,
    display_name: str,
    goal: float,
    calendar_period: calendar_period_pb2.CalendarPeriod,
    sli: monitoring_v3.ServiceLevelIndicator,
) -> None:
    """ServiceLevelObjective を作成または最新定義へ更新する（初回メトリクス伝播リトライ付き）。"""
    slo_name = f"{service_resource_name}/serviceLevelObjectives/{slo_id}"

    def _with_metric_propagation_retry(action_fn, verb: str) -> None:
        for attempt in range(1, 18):
            try:
                action_fn()
                print(f"[OK] SLO を{verb}しました: {slo_id} (goal={goal * 100:.1f}%)")
                return
            except gcp_exceptions.InvalidArgument as exc:
                if "cannot find metric" in str(exc).lower() and attempt < 17:
                    print(
                        f"  ... ログベースメトリクスの Cloud Monitoring 伝播待機中 "
                        f"({slo_id}, attempt={attempt}/17, 5秒後に再試行)..."
                    )
                    time.sleep(5)
                    continue
                raise

    try:
        client.get_service_level_objective(name=slo_name)
        slo_for_update = monitoring_v3.ServiceLevelObjective(
            name=slo_name,
            display_name=display_name,
            service_level_indicator=sli,
            goal=goal,
            calendar_period=calendar_period,
        )
        _with_metric_propagation_retry(
            lambda: client.update_service_level_objective(service_level_objective=slo_for_update),
            "更新",
        )
    except gcp_exceptions.NotFound:
        slo_for_create = monitoring_v3.ServiceLevelObjective(
            display_name=display_name,
            service_level_indicator=sli,
            goal=goal,
            calendar_period=calendar_period,
        )
        _with_metric_propagation_retry(
            lambda: client.create_service_level_objective(
                request=monitoring_v3.CreateServiceLevelObjectiveRequest(
                    parent=service_resource_name,
                    service_level_objective_id=slo_id,
                    service_level_objective=slo_for_create,
                )
            ),
            "作成",
        )


def delete_all_slo_resources(
    log_client: MetricsServiceV2Client,
    mon_client: monitoring_v3.ServiceMonitoringServiceClient,
    *,
    project_id: str,
    service_id: str,
    slo_ids: list[str],
) -> None:
    """セットアップした SLO・Service・ログベースメトリクスを安全に削除する。"""
    service_name = f"projects/{project_id}/services/{service_id}"
    for slo_id in slo_ids:
        slo_name = f"{service_name}/serviceLevelObjectives/{slo_id}"
        try:
            mon_client.delete_service_level_objective(name=slo_name)
            print(f"[DELETED] SLO を削除しました: {slo_id}")
        except gcp_exceptions.NotFound:
            print(f"[SKIP] SLO は既に存在しません: {slo_id}")

    try:
        mon_client.delete_service(name=service_name)
        print(f"[DELETED] Service Monitoring サービスを削除しました: {service_id}")
    except gcp_exceptions.NotFound:
        print(f"[SKIP] Service Monitoring サービスは既に存在しません: {service_id}")

    for metric_id in (ORDER_METRIC_ID, *LEGACY_METRIC_IDS):
        metric_resource_name = f"projects/{project_id}/metrics/{metric_id}"
        try:
            log_client.delete_log_metric(metric_name=metric_resource_name)
            print(f"[DELETED] ログベースメトリクスを削除しました: {metric_id}")
        except gcp_exceptions.NotFound:
            pass


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    load_dotenv(repo_root / ".env")

    parser = argparse.ArgumentParser(description="order-service 用 Cloud Monitoring SLO セットアップ・削除ツール")
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT", ""), help="Google Cloud プロジェクト ID")
    parser.add_argument("--cluster-name", default=os.getenv("GKE_CLUSTER_NAME", "sre-demo-cluster"))
    parser.add_argument("--cluster-location", default=os.getenv("GKE_CLUSTER_LOCATION", "asia-northeast1"))
    parser.add_argument("--service-id", default=os.getenv("SLO_SERVICE_ID", "order-service-slo"))
    parser.add_argument("--daily-slo-id", default=os.getenv("SLO_DAILY_ID", "order-availability-daily-99"))
    parser.add_argument("--monthly-slo-id", default=os.getenv("SLO_MONTHLY_ID", "order-availability-monthly-999"))
    parser.add_argument("--delete", action="store_true", help="作成した SLO・Service・ログメトリクスを削除する")
    args = parser.parse_args()

    if not args.project or args.project == "your-sandbox-project-id":
        print("エラー: GOOGLE_CLOUD_PROJECT 環境変数または --project を設定してください。", file=sys.stderr)
        return 1

    log_client = MetricsServiceV2Client()
    mon_client = monitoring_v3.ServiceMonitoringServiceClient()

    if args.delete:
        delete_all_slo_resources(
            log_client,
            mon_client,
            project_id=args.project,
            service_id=args.service_id,
            slo_ids=[args.daily_slo_id, args.monthly_slo_id],
        )
        return 0

    upsert_atomic_order_log_metric(log_client, project_id=args.project)

    service_resource_name = upsert_monitoring_service(
        mon_client,
        project_id=args.project,
        service_id=args.service_id,
        cluster_location=args.cluster_location,
        cluster_name=args.cluster_name,
    )

    sli = build_windows_availability_sli()
    upsert_slo(
        mon_client,
        service_resource_name=service_resource_name,
        slo_id=args.daily_slo_id,
        display_name="Order API 可用性 SLO - 日次 99.0% (許容 14.4 分/日・訓練前後比較用)",
        goal=0.99,
        calendar_period=calendar_period_pb2.CalendarPeriod.DAY,
        sli=sli,
    )
    upsert_slo(
        mon_client,
        service_resource_name=service_resource_name,
        slo_id=args.monthly_slo_id,
        display_name="Order API 可用性 SLO - 月次 99.9% (許容 43.2 分/月・本番運用基準)",
        goal=0.999,
        calendar_period=calendar_period_pb2.CalendarPeriod.MONTH,
        sli=sli,
    )

    console_url = f"https://console.cloud.google.com/monitoring/services/{args.service_id}?project={args.project}"
    print("\n========================================================================")
    print("SLO モニタリングのセットアップが完了しました。")
    print(f"Google Cloud コンソール確認 URL:\n  {console_url}")
    print("※ 初回作成直後はログベースメトリクスの集計反映まで約 2〜3 分お待ちください。")
    print("========================================================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

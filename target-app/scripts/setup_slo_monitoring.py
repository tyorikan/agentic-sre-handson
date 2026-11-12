#!/usr/bin/env python3
"""Cloud Logging カウンタメトリクスと Cloud Monitoring Service Monitoring SLO の冪等セットアップスクリプト。

作成・更新するリソース:
  1. Cloud Logging カウンタメトリクス:
     - order_requests_count      : POST /orders の全注文ログを単一メトリクスで集計し、
                                   ラベル `outcome = EXTRACT(severity)` (INFO=正常201 / ERROR,CRITICAL=異常5xx) で分類。
     - sre_agent_heartbeat_count : 常駐 SRE エージェントが出力する定期死活ログ (`event="agent_heartbeat"`) を集計。
  2. Cloud Monitoring Service & SLO (1分窓 Windows-based 可用性 SLO × 2本):
     - order-service-slo              : GKE Deployment (sre-demo-app/order-service) に対応する監視対象サービス
     - order-availability-daily-99    : カレンダー日次 99.0% (許容 14.4 分/日 — 訓練前後ゲージ・急燃焼検知用)
     - order-availability-monthly-999 : カレンダー月次 99.9% (許容 43.2 分/月 — 本番インパクト換算用)
  3. Cloud Monitoring AlertPolicy & NotificationChannel:
     - SLO Fast Burn-Rate アラート (`select_slo_burn_rate` 10分窓 > 10x) -> `sre-alert-stream` Pub/Sub 合流
     - SRE エージェント死活監視 Absence アラート (`sre_agent_heartbeat_count` 5分間不在) -> 人間向けメール通知チャネル

使い方:
  # セットアップ（作成または最新定義へ更新）
  python3 ./target-app/scripts/setup_slo_monitoring.py

  # クリーンアップ（第 6 章の削除手順で使用）
  python3 ./target-app/scripts/setup_slo_monitoring.py --delete
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from google.api import label_pb2, metric_pb2
from google.api_core import exceptions as gcp_exceptions
from google.cloud import monitoring_v3, pubsub_v1
from google.cloud.logging_v2.services.metrics_service_v2 import MetricsServiceV2Client
from google.cloud.logging_v2.types import logging_metrics
from google.protobuf import duration_pb2
from google.type import calendar_period_pb2

MANAGED_BY_LABEL = "agentic-sre-handson"
ORDER_METRIC_ID = "order_requests_count"
AGENT_HEARTBEAT_METRIC_ID = "sre_agent_heartbeat_count"
LEGACY_METRIC_IDS = ("order_requests_total", "order_requests_good")

ORDER_LOG_FILTER = (
    'resource.type="k8s_container"\n'
    'AND resource.labels.namespace_name="sre-demo-app"\n'
    'AND resource.labels.container_name="order-service"\n'
    'AND jsonPayload.service="order-service"\n'
    "AND (jsonPayload.order_id:* OR jsonPayload.error_code:*)"
)

AGENT_HEARTBEAT_LOG_FILTER = (
    'jsonPayload.event="agent_heartbeat"\n'
    'AND jsonPayload.status="healthy"'
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


def upsert_agent_heartbeat_log_metric(client: MetricsServiceV2Client, project_id: str) -> None:
    """常駐 SRE エージェントの定期 Heartbeat 構造化ログを集計するカウンタメトリクスを作成・更新する。"""
    parent = f"projects/{project_id}"
    metric_resource_name = f"projects/{project_id}/metrics/{AGENT_HEARTBEAT_METRIC_ID}"
    descriptor = metric_pb2.MetricDescriptor(
        metric_kind=metric_pb2.MetricDescriptor.MetricKind.DELTA,
        value_type=metric_pb2.MetricDescriptor.ValueType.INT64,
        unit="1",
    )
    metric = logging_metrics.LogMetric(
        name=AGENT_HEARTBEAT_METRIC_ID,
        description="常駐 SRE エージェントの定期死活 Heartbeat ログ件数",
        filter=AGENT_HEARTBEAT_LOG_FILTER,
        metric_descriptor=descriptor,
    )
    try:
        client.get_log_metric(metric_name=metric_resource_name)
        client.update_log_metric(metric_name=metric_resource_name, metric=metric)
        print(f"[OK] エージェント死活監視用ログメトリクスを更新しました: {AGENT_HEARTBEAT_METRIC_ID}")
    except gcp_exceptions.NotFound:
        client.create_log_metric(parent=parent, metric=metric)
        print(f"[OK] エージェント死活監視用ログメトリクスを作成しました: {AGENT_HEARTBEAT_METRIC_ID}")


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


def _with_metric_propagation_retry(label: str, action_fn, verb: str) -> None:
    """作成直後のログベースメトリクスが Cloud Monitoring へ伝播するまで再試行する。"""
    for attempt in range(1, 18):
        try:
            action_fn()
            print(f"[OK] {label} を{verb}しました。")
            return
        except gcp_exceptions.InvalidArgument as exc:
            if "cannot find metric" in str(exc).lower() and attempt < 17:
                print(
                    f"  ... ログベースメトリクスの Cloud Monitoring 伝播待機中 "
                    f"({label}, attempt={attempt}/17, 5秒後に再試行)..."
                )
                time.sleep(5)
                continue
            raise


def upsert_slo(
    client: monitoring_v3.ServiceMonitoringServiceClient,
    *,
    service_resource_name: str,
    slo_id: str,
    display_name: str,
    goal: float,
    calendar_period: calendar_period_pb2.CalendarPeriod,
    sli: monitoring_v3.ServiceLevelIndicator,
) -> str:
    """ServiceLevelObjective を作成または最新定義へ更新し、完全リソース名を返す。"""
    slo_name = f"{service_resource_name}/serviceLevelObjectives/{slo_id}"

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
            f"SLO ({slo_id}, goal={goal * 100:.1f}%)",
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
            f"SLO ({slo_id}, goal={goal * 100:.1f}%)",
            lambda: client.create_service_level_objective(
                request=monitoring_v3.CreateServiceLevelObjectiveRequest(
                    parent=service_resource_name,
                    service_level_objective_id=slo_id,
                    service_level_objective=slo_for_create,
                )
            ),
            "作成",
        )
    return slo_name


def resolve_project_number(project_id: str, service_resource_name: str = "") -> str | None:
    """Cloud Monitoring Notification Service Agent の特定に必要なプロジェクト番号を取得する。"""
    # Service Monitoring API が返す projects/{PROJECT_NUMBER}/services/... から直接抽出できる場合は最優先で使う
    parts = service_resource_name.split("/")
    if len(parts) >= 2 and parts[0] == "projects" and parts[1].isdigit():
        return parts[1]
    env_num = os.getenv("GOOGLE_CLOUD_PROJECT_NUMBER", "").strip()
    if env_num.isdigit():
        return env_num
    try:
        proc = subprocess.run(
            ["gcloud", "projects", "describe", project_id, "--format=value(projectNumber)"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        candidate = proc.stdout.strip()
        return candidate if proc.returncode == 0 and candidate.isdigit() else None
    except Exception:  # noqa: BLE001
        return None


def ensure_pubsub_notification_channel(
    channel_client: monitoring_v3.NotificationChannelServiceClient,
    pubsub_client: pubsub_v1.PublisherClient,
    *,
    project_id: str,
    alert_topic_id: str,
    service_resource_name: str = "",
) -> str | None:
    """既存の sre-alert-stream Pub/Sub トピックを Cloud Monitoring 通知チャネルとして登録し発行権限を付与する。"""
    parent = f"projects/{project_id}"
    topic_path = pubsub_client.topic_path(project_id, alert_topic_id)
    try:
        pubsub_client.get_topic(request={"topic": topic_path})
    except gcp_exceptions.NotFound:
        print(f"[SKIP] Pub/Sub トピック {alert_topic_id} が未作成のため、SLO アラートの Pub/Sub 結線はスキップしました。")
        return None

    channel_name: str | None = None
    for ch in channel_client.list_notification_channels(name=parent):
        if ch.type_ == "pubsub" and (ch.labels or {}).get("topic") == topic_path:
            channel_name = ch.name
            print(f"[OK] Monitoring Pub/Sub 通知チャネルを確認しました: {channel_name}")
            break

    if channel_name is None:
        created = channel_client.create_notification_channel(
            name=parent,
            notification_channel=monitoring_v3.NotificationChannel(
                type_="pubsub",
                display_name=f"SRE Agent Alert Stream ({alert_topic_id})",
                labels={"topic": topic_path},
                user_labels={"managed_by": MANAGED_BY_LABEL, "purpose": "slo_burn_trigger"},
                enabled=True,
            ),
        )
        channel_name = created.name
        print(f"[OK] Monitoring Pub/Sub 通知チャネルを作成しました: {channel_name}")

    project_number = resolve_project_number(project_id, service_resource_name)
    if project_number:
        member = f"serviceAccount:service-{project_number}@gcp-sa-monitoring-notification.iam.gserviceaccount.com"
        for attempt in range(1, 6):
            try:
                policy = pubsub_client.get_iam_policy(request={"resource": topic_path})
                role = "roles/pubsub.publisher"
                binding = next((b for b in policy.bindings if b.role == role), None)
                if binding is None:
                    policy.bindings.add(role=role, members=[member])
                    pubsub_client.set_iam_policy(request={"resource": topic_path, "policy": policy})
                elif member not in binding.members:
                    binding.members.append(member)
                    pubsub_client.set_iam_policy(request={"resource": topic_path, "policy": policy})
                print(f"[OK] Monitoring 通知サービスエージェントへ Pub/Sub 発行権限を確認しました: {member}")
                break
            except gcp_exceptions.InvalidArgument:
                if attempt < 5:
                    time.sleep(3)
                    continue
                print(f"[WARN] サービスエージェント {member} の IAM 伝播待機がタイムアウトしました。")
    return channel_name


def ensure_human_email_channels(
    channel_client: monitoring_v3.NotificationChannelServiceClient,
    *,
    project_id: str,
    notify_email_to: str,
) -> list[str]:
    """エージェント自身の停止時に人間へ直接通知するためのメール通知チャネルを登録する。"""
    emails = [addr.strip() for addr in notify_email_to.split(",") if addr.strip() and "@" in addr]
    if not emails:
        return []
    parent = f"projects/{project_id}"
    existing_by_email = {
        (ch.labels or {}).get("email_address", "").strip().lower(): ch.name
        for ch in channel_client.list_notification_channels(name=parent)
        if ch.type_ == "email"
    }
    channel_names: list[str] = []
    for addr in emails[:5]:
        found = existing_by_email.get(addr.lower())
        if found:
            channel_names.append(found)
            continue
        created = channel_client.create_notification_channel(
            name=parent,
            notification_channel=monitoring_v3.NotificationChannel(
                type_="email",
                display_name=f"SRE Operator Direct Alert ({addr})",
                labels={"email_address": addr},
                user_labels={"managed_by": MANAGED_BY_LABEL, "purpose": "agent_heartbeat_human"},
                enabled=True,
            ),
        )
        channel_names.append(created.name)
        print(f"[OK] 人間向け Monitoring メール通知チャネルを作成しました: {addr}")
    return channel_names


def upsert_alert_policy_by_role(
    policy_client: monitoring_v3.AlertPolicyServiceClient,
    *,
    project_id: str,
    policy_role: str,
    policy: monitoring_v3.AlertPolicy,
) -> str:
    """user_labels.policy_role をキーに AlertPolicy を冪等に作成または更新する。"""
    parent = f"projects/{project_id}"
    existing = next(
        (
            p
            for p in policy_client.list_alert_policies(name=parent)
            if (p.user_labels or {}).get("managed_by") == MANAGED_BY_LABEL
            and (p.user_labels or {}).get("policy_role") == policy_role
        ),
        None,
    )
    if existing is not None:
        policy.name = existing.name
        _with_metric_propagation_retry(
            f"AlertPolicy ({policy_role})",
            lambda: policy_client.update_alert_policy(alert_policy=policy),
            "更新",
        )
        return existing.name

    created_holder: list[str] = []

    def _create() -> None:
        created = policy_client.create_alert_policy(name=parent, alert_policy=policy)
        created_holder.append(created.name)

    _with_metric_propagation_retry(f"AlertPolicy ({policy_role})", _create, "作成")
    return created_holder[0]


def build_slo_fast_burn_alert_policy(
    *,
    slo_resource_name: str,
    pubsub_channel_name: str | None,
    burn_rate_threshold: float = 10.0,
) -> monitoring_v3.AlertPolicy:
    """ERROR ログが出ない症状悪化も検知できるよう、SLO 10分窓の急燃焼アラートを定義する。"""
    return monitoring_v3.AlertPolicy(
        display_name="Order Service SLO Fast Burn-Rate Alert (10m > 10x)",
        combiner=monitoring_v3.AlertPolicy.ConditionCombinerType.OR,
        conditions=[
            monitoring_v3.AlertPolicy.Condition(
                display_name="10m SLO burn rate > 10x",
                condition_threshold=monitoring_v3.AlertPolicy.Condition.MetricThreshold(
                    filter=f'select_slo_burn_rate("{slo_resource_name}", "10m")',
                    comparison=monitoring_v3.ComparisonType.COMPARISON_GT,
                    threshold_value=burn_rate_threshold,
                    duration=duration_pb2.Duration(seconds=0),
                ),
            )
        ],
        notification_channels=[pubsub_channel_name] if pubsub_channel_name else [],
        alert_strategy=monitoring_v3.AlertPolicy.AlertStrategy(
            auto_close=duration_pb2.Duration(seconds=1800),
        ),
        severity=monitoring_v3.AlertPolicy.Severity.ERROR,
        user_labels={
            "managed_by": MANAGED_BY_LABEL,
            "policy_role": "slo_fast_burn",
            "namespace": "sre-demo-app",
            "service_name": "order-service",
            "error_code": "slo_fast_burn_rate",
        },
        documentation=monitoring_v3.AlertPolicy.Documentation(
            content=(
                "order-service の直近 10 分間の Error Budget 燃焼率が許容ペースの 10 倍を超過しました。"
                "ERROR ログを伴わない症状ベースの異常（高レイテンシや 5xx 急増）を含め SRE エージェントが相関調査を実施します。"
            ),
            mime_type="text/markdown",
        ),
    )


def build_agent_heartbeat_absence_policy(
    *,
    human_channel_names: list[str],
    absence_seconds: int = 300,
) -> monitoring_v3.AlertPolicy:
    """常駐 SRE エージェント自身の Heartbeat が 5 分間途絶した際に人間へ直接通知するポリシーを定義する。"""
    return monitoring_v3.AlertPolicy(
        display_name="SRE Autonomous Agent Heartbeat Absence (5m)",
        combiner=monitoring_v3.AlertPolicy.ConditionCombinerType.OR,
        conditions=[
            monitoring_v3.AlertPolicy.Condition(
                display_name="No SRE agent heartbeat logs for 5 minutes",
                condition_absent=monitoring_v3.AlertPolicy.Condition.MetricAbsence(
                    filter=(
                        f'metric.type="logging.googleapis.com/user/{AGENT_HEARTBEAT_METRIC_ID}" '
                        'AND resource.type="cloud_run_instance"'
                    ),
                    aggregations=[
                        monitoring_v3.Aggregation(
                            alignment_period=duration_pb2.Duration(seconds=60),
                            per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_SUM,
                            cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_SUM,
                        )
                    ],
                    duration=duration_pb2.Duration(seconds=absence_seconds),
                ),
            )
        ],
        notification_channels=human_channel_names,
        alert_strategy=monitoring_v3.AlertPolicy.AlertStrategy(
            auto_close=duration_pb2.Duration(seconds=3600),
        ),
        severity=monitoring_v3.AlertPolicy.Severity.CRITICAL,
        user_labels={
            "managed_by": MANAGED_BY_LABEL,
            "policy_role": "agent_heartbeat_absence",
        },
        documentation=monitoring_v3.AlertPolicy.Documentation(
            content=(
                "常駐 SRE エージェント (sre-autonomous-agent) の定期 Heartbeat ログが 5 分以上途絶しています。"
                "Cloud Run instances の状態または Pub/Sub 購読スレッドの異常を確認してください。"
            ),
            mime_type="text/markdown",
        ),
    )


def delete_all_slo_resources(
    log_client: MetricsServiceV2Client,
    mon_client: monitoring_v3.ServiceMonitoringServiceClient,
    policy_client: monitoring_v3.AlertPolicyServiceClient | None = None,
    channel_client: monitoring_v3.NotificationChannelServiceClient | None = None,
    *,
    project_id: str,
    service_id: str,
    slo_ids: list[str],
) -> None:
    """セットアップしたアラート・通知チャネル・SLO・Service・ログベースメトリクスを安全に削除する。"""
    parent = f"projects/{project_id}"
    if policy_client is not None:
        for policy in policy_client.list_alert_policies(name=parent):
            if (policy.user_labels or {}).get("managed_by") == MANAGED_BY_LABEL:
                policy_client.delete_alert_policy(name=policy.name)
                print(f"[DELETED] AlertPolicy を削除しました: {policy.display_name}")

    if channel_client is not None:
        for ch in channel_client.list_notification_channels(name=parent):
            if (ch.user_labels or {}).get("managed_by") == MANAGED_BY_LABEL:
                try:
                    channel_client.delete_notification_channel(name=ch.name, force=True)
                    print(f"[DELETED] NotificationChannel を削除しました: {ch.display_name}")
                except gcp_exceptions.GoogleAPICallError as exc:
                    print(f"[WARN] NotificationChannel 削除をスキップしました ({ch.display_name}): {exc}")

    service_name = f"{parent}/services/{service_id}"
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

    for metric_id in (ORDER_METRIC_ID, AGENT_HEARTBEAT_METRIC_ID, *LEGACY_METRIC_IDS):
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
    parser.add_argument("--alert-topic", default=os.getenv("ALERT_TOPIC", "sre-alert-stream"))
    parser.add_argument("--notify-email-to", default=os.getenv("NOTIFY_EMAIL_TO", ""))
    parser.add_argument("--delete", action="store_true", help="作成したアラート・通知チャネル・SLO・Service・ログメトリクスを削除する")
    args = parser.parse_args()

    if not args.project or args.project == "your-sandbox-project-id":
        print("エラー: GOOGLE_CLOUD_PROJECT 環境変数または --project を設定してください。", file=sys.stderr)
        return 1

    log_client = MetricsServiceV2Client()
    mon_client = monitoring_v3.ServiceMonitoringServiceClient()
    policy_client = monitoring_v3.AlertPolicyServiceClient()
    channel_client = monitoring_v3.NotificationChannelServiceClient()

    if args.delete:
        delete_all_slo_resources(
            log_client,
            mon_client,
            policy_client,
            channel_client,
            project_id=args.project,
            service_id=args.service_id,
            slo_ids=[args.daily_slo_id, args.monthly_slo_id],
        )
        return 0

    upsert_atomic_order_log_metric(log_client, project_id=args.project)
    upsert_agent_heartbeat_log_metric(log_client, project_id=args.project)

    service_resource_name = upsert_monitoring_service(
        mon_client,
        project_id=args.project,
        service_id=args.service_id,
        cluster_location=args.cluster_location,
        cluster_name=args.cluster_name,
    )

    sli = build_windows_availability_sli()
    daily_slo_name = upsert_slo(
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

    pubsub_client = pubsub_v1.PublisherClient()
    pubsub_channel = ensure_pubsub_notification_channel(
        channel_client,
        pubsub_client,
        project_id=args.project,
        alert_topic_id=args.alert_topic,
        service_resource_name=service_resource_name,
    )
    upsert_alert_policy_by_role(
        policy_client,
        project_id=args.project,
        policy_role="slo_fast_burn",
        policy=build_slo_fast_burn_alert_policy(
            slo_resource_name=daily_slo_name,
            pubsub_channel_name=pubsub_channel,
        ),
    )

    human_channels = ensure_human_email_channels(
        channel_client,
        project_id=args.project,
        notify_email_to=args.notify_email_to,
    )
    upsert_alert_policy_by_role(
        policy_client,
        project_id=args.project,
        policy_role="agent_heartbeat_absence",
        policy=build_agent_heartbeat_absence_policy(human_channel_names=human_channels),
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

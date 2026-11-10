"""常駐自律型 SRE エージェント エントリポイント (`main.py`)。

Cloud Run instances 上で単一常駐プロセスとして稼働し、以下の 2 つの外向きストリームを並行処理します。
  1. Cloud Pub/Sub StreamingPull によるアラート・異常ログの常時待ち受け
  2. Slack Socket Mode による双方向 Interactive 承認イベントの常時待ち受け

着信 HTTP ポートを一切開けずに動作するため、`--ingress internal --no-default-url` の
完全閉域インスタンス構成で安全に運用できます。
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
import uuid
from typing import Any

from google.cloud import pubsub_v1

from .config import AgentSettings, load_settings
from .dedup_store import BigQueryEventStoreBackend, IncidentGuardrailManager
from .models import AlertEvent
from .remediation_tools import GkeAppsV1RemediationExecutor, SafeRemediationService
from .sandbox_runner import SandboxRunner
from .slack_socket import SlackHitlCoordinator
from .vector_kb import BigQueryVectorKnowledgeBase
from .workflow import SreIncidentOrchestrator


def _log_json(severity: str, message: str, **extra: Any) -> None:
    payload = {"severity": severity, "message": message, **extra}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def parse_pubsub_payload(raw_bytes: bytes) -> AlertEvent:
    """Cloud Monitoring アラート通知、Cloud Logging シンク、または直接投入された AlertEvent JSON を正規化する。"""
    data = json.loads(raw_bytes.decode("utf-8"))

    # 1. Cloud Monitoring Incident Notification 形式
    if "incident" in data and isinstance(data["incident"], dict):
        inc = data["incident"]
        labels = inc.get("resource", {}).get("labels", {})
        metric_labels = inc.get("metric", {}).get("labels", {})
        return AlertEvent(
            incident_id=str(inc.get("incident_id", f"mon-{uuid.uuid4().hex[:8]}")),
            namespace=labels.get("namespace_name", "sre-demo-app"),
            service_name=labels.get("container_name") or metric_labels.get("service", "order-service"),
            error_code=inc.get("policy_name", "MONITORING_ALERT"),
            severity=str(inc.get("severity", "ERROR")).upper(),
            summary=inc.get("summary", "Cloud Monitoring alert triggered"),
        )

    # 2. Cloud Logging LogEntry Sink 形式 (jsonPayload を含む場合)
    if "jsonPayload" in data and isinstance(data["jsonPayload"], dict):
        jp = data["jsonPayload"]
        res_labels = data.get("resource", {}).get("labels", {})
        return AlertEvent(
            incident_id=data.get("insertId", f"log-{uuid.uuid4().hex[:8]}"),
            namespace=jp.get("namespace") or res_labels.get("namespace_name", "sre-demo-app"),
            service_name=jp.get("service") or res_labels.get("container_name", "order-service"),
            error_code=jp.get("error_code", "STRUCTURED_LOG_ERROR"),
            severity=str(data.get("severity", jp.get("severity", "ERROR"))).upper(),
            summary=jp.get("message", "Error log routed via Log Sink"),
            recent_deployment_within_30m=bool(jp.get("introduced_in")),
        )

    # 3. 標準 AlertEvent JSON 形式
    return AlertEvent.model_validate(data)


async def process_incoming_alert(
    alert: AlertEvent,
    guardrails: IncidentGuardrailManager,
    orchestrator: SreIncidentOrchestrator,
    slack_coordinator: SlackHitlCoordinator,
) -> bool:
    """単一アラートをクールダウン判定の上で処理する（スキップ時は False、実行時は True）。"""
    in_cooldown, reason = guardrails.is_in_cooldown(alert)
    if in_cooldown:
        _log_json("INFO", "クールダウン中のためアラートを重複排除スキップしました。", incident_id=alert.incident_id, reason=reason)
        return False

    _log_json("INFO", "新規インシデントの自律相関調査を開始します。", incident_id=alert.incident_id, error_code=alert.error_code)
    result = await orchestrator.handle_alert(alert)
    await slack_coordinator.notify_workflow_result(result)
    _log_json(
        "INFO",
        "インシデントの初期ワークフローが完了しました。",
        incident_id=alert.incident_id,
        decision=result.decision.decision,
        paused_for_approval=result.paused_for_approval,
    )
    return True


async def run_daemon(settings: AgentSettings) -> None:
    """Pub/Sub StreamingPull と Slack Socket Mode を並行稼働させる常駐メインループ。"""
    event_store = BigQueryEventStoreBackend(settings)
    guardrails = IncidentGuardrailManager(settings, event_store)
    sandbox_runner = SandboxRunner(settings)
    kb = BigQueryVectorKnowledgeBase(settings)
    remediation_service = SafeRemediationService(settings, GkeAppsV1RemediationExecutor(settings))

    orchestrator = SreIncidentOrchestrator(
        settings=settings,
        sandbox_runner=sandbox_runner,
        knowledge_base=kb,
        guardrail_manager=guardrails,
        remediation_service=remediation_service,
    )
    slack_coordinator = SlackHitlCoordinator(settings, orchestrator)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await slack_coordinator.start_socket_mode()
    _log_json("INFO", "Slack Socket Mode 接続準備完了（未設定時はローカルログモード）。")

    subscriber = pubsub_v1.SubscriberClient()
    sub_path = subscriber.subscription_path(settings.project_id, settings.alert_subscription)

    def _callback(message: pubsub_v1.subscriber.message.Message) -> None:
        try:
            alert = parse_pubsub_payload(message.data)
            fut = asyncio.run_coroutine_threadsafe(
                process_incoming_alert(alert, guardrails, orchestrator, slack_coordinator),
                loop,
            )
            fut.result(timeout=120)
            message.ack()
        except Exception as exc:  # noqa: BLE001
            _log_json("ERROR", f"アラート処理中に例外が発生しました: {exc}")
            message.nack()

    streaming_pull_future = subscriber.subscribe(
        sub_path,
        callback=_callback,
        flow_control=pubsub_v1.types.FlowControl(max_messages=5),
    )
    _log_json("INFO", "常駐自律型 SRE エージェントが稼働開始しました。", subscription=sub_path, region=settings.region)

    try:
        await stop_event.wait()
    finally:
        _log_json("INFO", "シャットダウンシグナルを受信しました。接続を安全に終了します...")
        streaming_pull_future.cancel()
        subscriber.close()
        await slack_coordinator.close_socket_mode()


def main() -> None:
    settings = load_settings()
    asyncio.run(run_daemon(settings))


if __name__ == "__main__":
    main()

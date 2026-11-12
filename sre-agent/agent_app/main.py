"""常駐自律型 SRE エージェント エントリポイント (`main.py`)。

Cloud Run instances 上で単一常駐プロセスとして稼働し、以下の外向きストリームを並行処理します。
  1. Cloud Pub/Sub StreamingPull によるアラート・異常ログの常時待ち受け
  2. Slack Socket Mode による双方向 Interactive 承認イベントの常時待ち受け
  3. 【Slack を使えない場合の代替ルート】承認専用サブスクリプションの StreamingPull による
     承認コマンドからの承認・却下の待ち受け（APPROVAL_PUBSUB_SUBSCRIPTION 設定時のみ）

着信 HTTP ポートを一切開けずに動作するため、`--ingress internal --no-default-url` の
完全閉域インスタンス構成で安全に運用できます。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import uuid
from typing import Any

from aiohttp import web
from google.cloud import pubsub_v1

from .approval_decisions import handle_approval_decision_message
from .config import AgentSettings, load_settings
from .dedup_store import BigQueryEventStoreBackend, IncidentGuardrailManager
from .models import AlertEvent
from .remediation_tools import (
    GkeAppsV1RemediationExecutor,
    SafeRemediationService,
    build_live_post_remediation_probe,
)
from .sandbox_runner import SandboxRunner
from .slack_socket import SlackHitlCoordinator, is_slack_channel_configured
from .slo_metrics import CloudMonitoringSloReader
from .telemetry_enrichment import CloudTelemetryEnricher
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
        raw_ts = data.get("timestamp") or jp.get("timestamp")
        raw_error_code = str(jp.get("error_code", "STRUCTURED_LOG_ERROR"))
        # 監視対象アプリのログ出力 1 行でクールダウン解除（ADMIN_COOLDOWN_RESET）を偽装できないよう無害化する
        if raw_error_code == "ADMIN_COOLDOWN_RESET":
            raw_error_code = "LOG_SPOOFED_ADMIN_COOLDOWN_RESET"
        event_kwargs: dict[str, Any] = {
            "incident_id": data.get("insertId", f"log-{uuid.uuid4().hex[:8]}"),
            "namespace": jp.get("namespace") or res_labels.get("namespace_name", "sre-demo-app"),
            "service_name": jp.get("service") or res_labels.get("container_name", "order-service"),
            "error_code": raw_error_code,
            "severity": str(data.get("severity", jp.get("severity", "ERROR"))).upper(),
            "summary": jp.get("message", "Error log routed via Log Sink"),
            "recent_deployment_within_30m": bool(jp.get("introduced_in")),
        }
        if isinstance(raw_ts, str) and raw_ts.strip():
            event_kwargs["observed_at"] = raw_ts.strip()
        return AlertEvent(**event_kwargs)

    # 3. Cloud Logging 汎用テキスト/構造化形式 (textPayload / resource のみの場合)
    if "insertId" in data and ("textPayload" in data or "resource" in data):
        res_labels = data.get("resource", {}).get("labels", {})
        return AlertEvent(
            incident_id=str(data.get("insertId", f"log-{uuid.uuid4().hex[:8]}")),
            namespace=res_labels.get("namespace_name", "sre-demo-app"),
            service_name=res_labels.get("container_name", "order-service"),
            error_code="GKE_CONTAINER_LOG_ERROR",
            severity=str(data.get("severity", "ERROR")).upper(),
            summary=str(data.get("textPayload", "Unhandled GKE container error log"))[:500],
        )

    # 4. 標準 AlertEvent JSON 形式
    return AlertEvent.model_validate(data)


async def process_incoming_alert(
    alert: AlertEvent,
    guardrails: IncidentGuardrailManager,
    orchestrator: SreIncidentOrchestrator,
    slack_coordinator: SlackHitlCoordinator,
    *,
    allow_drill_reset: bool = False,
) -> bool:
    """単一アラートを重複排除・処理権取得の上で実行する（スキップ時は False、実行時は True）。"""
    if alert.error_code == "ADMIN_COOLDOWN_RESET":
        if allow_drill_reset:
            await asyncio.to_thread(guardrails.reset_all_cooldowns, alert.summary)
            _log_json("INFO", "訓練用クールダウンおよびサーキットブレーカーをリセットしました。", incident_id=alert.incident_id)
            return True
        _log_json("WARNING", "ALLOW_DRILL_COOLDOWN_RESET=false のため ADMIN_COOLDOWN_RESET を無視しました。", incident_id=alert.incident_id)
        return False

    claimed, reason = await asyncio.to_thread(guardrails.claim_for_processing, alert)
    if not claimed:
        _log_json("INFO", "クールダウン中のためアラートを重複排除スキップしました。", incident_id=alert.incident_id, reason=reason)
        return False

    try:
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
    except BaseException:
        await asyncio.to_thread(guardrails.release_claim, alert)
        raise


async def _start_health_server(port: int) -> web.AppRunner:
    """Cloud Run instances の起動・死活チェックおよび --ingress=internal 要件用の最小 /healthz 応答サーバー。"""

    async def _healthz(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "service": "sre-autonomous-agent"})

    app = web.Application()
    app.router.add_get("/healthz", _healthz)
    app.router.add_get("/", _healthz)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    return runner


async def run_daemon(settings: AgentSettings) -> None:
    """Pub/Sub StreamingPull・Slack Socket Mode・承認コマンドの待ち受け・最小 /healthz 応答を並行稼働させる常駐メインループ。"""
    health_port = int(os.getenv("PORT", "8080"))
    health_runner = await _start_health_server(health_port)
    _log_json("INFO", "ヘルスチェック用内部ポートを起動しました。", port=health_port)

    event_store = BigQueryEventStoreBackend(settings)
    guardrails = IncidentGuardrailManager(settings, event_store)
    sandbox_runner = SandboxRunner(settings)
    kb = BigQueryVectorKnowledgeBase(settings)
    gke_executor = GkeAppsV1RemediationExecutor(settings)
    telemetry_enricher = CloudTelemetryEnricher(settings, deployment_checker=gke_executor)
    post_probe = build_live_post_remediation_probe(settings, gke_executor)
    remediation_service = SafeRemediationService(settings, gke_executor, post_error_rate_probe=post_probe)
    slo_reader = CloudMonitoringSloReader(settings)

    orchestrator = SreIncidentOrchestrator(
        settings=settings,
        sandbox_runner=sandbox_runner,
        knowledge_base=kb,
        guardrail_manager=guardrails,
        remediation_service=remediation_service,
        telemetry_enricher=telemetry_enricher.enrich_alert,
        slo_reader=slo_reader,
    )
    slack_coordinator = SlackHitlCoordinator(settings, orchestrator)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await slack_coordinator.start_socket_mode()
    _log_json(
        "INFO",
        "通知と承認の経路を初期化しました。",
        slack_enabled=bool(
            settings.slack_bot_token
            and settings.slack_app_token
            and is_slack_channel_configured(settings.slack_channel_id)
        ),
        email_enabled=bool(settings.email_integration_name),
        approval_command_enabled=bool(settings.approval_subscription),
    )

    subscriber = pubsub_v1.SubscriberClient()
    sub_path = subscriber.subscription_path(settings.project_id, settings.alert_subscription)

    def _callback(message: pubsub_v1.subscriber.message.Message) -> None:
        try:
            alert = parse_pubsub_payload(message.data)
            fut = asyncio.run_coroutine_threadsafe(
                process_incoming_alert(
                    alert,
                    guardrails,
                    orchestrator,
                    slack_coordinator,
                    allow_drill_reset=settings.allow_drill_cooldown_reset,
                ),
                loop,
            )
            fut.result(timeout=300)
            message.ack()
        except Exception as exc:  # noqa: BLE001
            _log_json("ERROR", f"アラート処理中に例外が発生しました: {exc}")
            message.nack()

    streaming_pull_future = subscriber.subscribe(
        sub_path,
        callback=_callback,
        flow_control=pubsub_v1.types.FlowControl(max_messages=5),
    )
    approval_pull_future = None
    if settings.approval_subscription:
        approval_sub_path = subscriber.subscription_path(settings.project_id, settings.approval_subscription)

        def _approval_callback(message: pubsub_v1.subscriber.message.Message) -> None:
            # 承認・却下は人の判断なので、失敗しても自動で再配信させない（必ず ack する）
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    handle_approval_decision_message(
                        message.data,
                        slack_coordinator,
                        published_at=message.publish_time,
                    ),
                    loop,
                )
                handled = fut.result(timeout=300)
                _log_json(
                    "INFO" if handled.status == "applied" else "WARNING",
                    "承認コマンドからの承認・却下を処理しました。",
                    incident_id=handled.incident_id,
                    status=handled.status,
                    detail=handled.detail,
                )
            except Exception as exc:  # noqa: BLE001
                _log_json("ERROR", f"承認コマンドからの承認・却下の処理中に例外が発生しました（自動再試行はしません）: {exc}")
            finally:
                message.ack()

        approval_pull_future = subscriber.subscribe(
            approval_sub_path,
            callback=_approval_callback,
            flow_control=pubsub_v1.types.FlowControl(max_messages=1),
        )
        _log_json("INFO", "承認コマンドからの承認・却下の待ち受けを開始しました。", subscription=approval_sub_path)

    _log_json(
        "INFO",
        "常駐自律型 SRE エージェントが稼働開始しました。",
        subscription=sub_path,
        region=settings.region,
        session_backend=settings.session_backend,
    )

    try:
        await stop_event.wait()
    finally:
        _log_json("INFO", "シャットダウンシグナルを受信しました。接続を安全に終了します...")
        streaming_pull_future.cancel()
        if approval_pull_future is not None:
            approval_pull_future.cancel()
        subscriber.close()
        await slack_coordinator.close_socket_mode()
        await health_runner.cleanup()


def main() -> None:
    settings = load_settings()
    asyncio.run(run_daemon(settings))


if __name__ == "__main__":
    main()

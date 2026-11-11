"""監視対象サンプル注文マイクロサービス (Order API)。

GKE クラスタ上で稼働し、構造化 JSON ログと HTTP ステータスメトリクスを出力します。
ハンズオンの障害訓練用に、以下の 3 つの障害モードを環境変数または管理エンドポイントから再現できます。

1. TRANSIENT_NET_ERR: 下流決済ゲートウェイの一時的な通信瞬断（指定秒数後に自然回復）
2. MISSING_CONFIG_BUG: v2 デプロイ時の必須設定キー（PAYMENT_CURRENCY_PRECISION）欠落バグ（ロールバックで解消）
3. DB_POOL_EXHAUSTED: データベースコネクションプール枯渇・ロック待ち（未知/高リスク障害として人間承認を要求）
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

app = FastAPI(title="Order Service (Agentic SRE Target App)", version=os.getenv("APP_VERSION", "v1.0.0"))

# 実行時障害注入ステート（Pod 単位で保持）
_FAULT_STATE: dict[str, Any] = {
    "mode": os.getenv("INITIAL_FAULT_MODE", "NONE"),  # NONE | TRANSIENT_NET_ERR | DB_POOL_EXHAUSTED
    "expires_at": 0.0,
}


def _emit_structured_log(
    severity: str,
    message: str,
    *,
    error_code: str | None = None,
    trace_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Cloud Logging がそのまま解析できる構造化 JSON ログを標準出力へ書き出す。"""
    payload: dict[str, Any] = {
        "severity": severity,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": "order-service",
        "version": os.getenv("APP_VERSION", "v1.0.0"),
        "pod_name": os.getenv("HOSTNAME", "local-pod"),
        "namespace": os.getenv("POD_NAMESPACE", "sre-demo-app"),
    }
    if error_code:
        payload["error_code"] = error_code
    if trace_id:
        payload["logging.googleapis.com/trace"] = trace_id
    if extra:
        payload.update(extra)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _active_fault_mode() -> str:
    """有効期限を考慮して現在の障害モードを返す。"""
    # v2 バグ入りイメージの再現: PAYMENT_CURRENCY_PRECISION 環境変数が欠落している場合
    if os.getenv("APP_VERSION") == "v2.0.0-buggy" and not os.getenv("PAYMENT_CURRENCY_PRECISION"):
        return "MISSING_CONFIG_BUG"

    mode = _FAULT_STATE["mode"]
    expires_at = _FAULT_STATE["expires_at"]
    if mode != "NONE" and expires_at > 0 and time.time() > expires_at:
        _FAULT_STATE["mode"] = "NONE"
        _FAULT_STATE["expires_at"] = 0.0
        _emit_structured_log("INFO", f"一時的障害モード {mode} が自然回復しました。")
        return "NONE"
    return mode


class OrderRequest(BaseModel):
    """注文作成リクエスト。"""

    item_id: str = "SKU-1001"
    quantity: int = 1
    amount_jpy: int = 4800


class FaultInjectionRequest(BaseModel):
    """訓練用の障害注入リクエスト。"""

    mode: str  # NONE | TRANSIENT_NET_ERR | DB_POOL_EXHAUSTED
    duration_seconds: int = 90


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Kubernetes Liveness / Readiness Probe 用ヘルスチェック。"""
    return {"status": "ok", "version": os.getenv("APP_VERSION", "v1.0.0")}


@app.post("/orders")
async def create_order(req: OrderRequest, request: Request, response: Response) -> dict[str, Any]:
    """注文を処理し、アクティブな障害モードに応じたエラーまたは正常応答を返す。"""
    trace_id = request.headers.get("X-Cloud-Trace-Context", uuid.uuid4().hex)
    fault = _active_fault_mode()

    if fault == "TRANSIENT_NET_ERR":
        _emit_structured_log(
            "ERROR",
            "Upstream payment gateway connection reset by peer during TLS handshake (transient spike)",
            error_code="UPSTREAM_CONN_RESET_503",
            trace_id=trace_id,
            extra={"upstream_host": "api.payment-partner.example", "retryable": True},
        )
        raise HTTPException(status_code=503, detail="Upstream payment gateway temporarily unavailable")

    if fault == "MISSING_CONFIG_BUG":
        _emit_structured_log(
            "ERROR",
            "KeyError: 'PAYMENT_CURRENCY_PRECISION' missing after deployment rollout v2.0.0-buggy",
            error_code="CONFIG_KEY_ERROR_500",
            trace_id=trace_id,
            extra={"missing_env": "PAYMENT_CURRENCY_PRECISION", "introduced_in": "v2.0.0-buggy"},
        )
        raise HTTPException(status_code=500, detail="Internal configuration error in v2.0.0-buggy")

    if fault == "DB_POOL_EXHAUSTED":
        _emit_structured_log(
            "CRITICAL",
            "OperationalError: FATAL: remaining connection slots are reserved for superuser; active lock wait on table 'orders_ledger'",
            error_code="DB_CONN_SLOTS_EXHAUSTED_500",
            trace_id=trace_id,
            extra={"db_wait_event": "Lock:transactionid", "active_connections": 200, "max_connections": 200},
        )
        raise HTTPException(status_code=500, detail="Database connection pool exhausted due to lock contention")

    order_id = f"ORD-{uuid.uuid4().hex[:10].upper()}"
    _emit_structured_log(
        "INFO",
        "注文処理が正常に完了しました。",
        trace_id=trace_id,
        extra={"order_id": order_id, "item_id": req.item_id, "amount_jpy": req.amount_jpy},
    )
    response.status_code = 201
    return {"order_id": order_id, "status": "CONFIRMED", "version": os.getenv("APP_VERSION", "v1.0.0")}


@app.post("/admin/fault")
async def configure_fault(fault_req: FaultInjectionRequest) -> dict[str, Any]:
    """ハンズオンの障害注入スクリプトから一時的な障害状態をセットする。"""
    allowed = {"NONE", "TRANSIENT_NET_ERR", "DB_POOL_EXHAUSTED"}
    if fault_req.mode not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported mode: {fault_req.mode}")

    _FAULT_STATE["mode"] = fault_req.mode
    _FAULT_STATE["expires_at"] = (
        time.time() + fault_req.duration_seconds if fault_req.mode != "NONE" and fault_req.duration_seconds > 0 else 0.0
    )
    _emit_structured_log(
        "WARNING",
        f"訓練用障害モードを変更しました: mode={fault_req.mode}, duration_seconds={fault_req.duration_seconds}",
        extra={"fault_mode": fault_req.mode, "duration_seconds": fault_req.duration_seconds},
    )
    return {"active_mode": _FAULT_STATE["mode"], "expires_at": _FAULT_STATE["expires_at"]}

"""重複排除・クールダウン・サーキットブレーカー・監視対象アプリの単体テスト。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app as target_app
from agent_app.config import AgentSettings
from agent_app.dedup_store import IncidentGuardrailManager, InMemoryEventStoreBackend
from agent_app.models import AlertEvent
from agent_app.main import parse_pubsub_payload


def test_cooldown_deduplication() -> None:
    """クールダウン期間内の同一 fingerprint アラートが確実にスキップされること。"""
    settings = AgentSettings(cooldown_minutes=15)
    store = InMemoryEventStoreBackend()
    mgr = IncidentGuardrailManager(settings, store)

    t0 = datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)
    alert = AlertEvent(
        incident_id="INC-TEST-1",
        namespace="sre-demo-app",
        service_name="order-service",
        error_code="UPSTREAM_CONN_RESET_503",
        summary="Transient TLS reset",
    )

    in_cd, _ = mgr.is_in_cooldown(alert, now=t0)
    assert in_cd is False

    mgr.record_outcome(
        alert=alert,
        decision="OBSERVE",
        remediation_type="NONE",
        matched_runbook_id="INC-013",
        similarity_score=0.94,
        verification_passed=True,
        summary="Observed transient error",
        now=t0,
    )

    # 5 分後の重複アラートはクールダウン中と判定される
    in_cd_5m, reason = mgr.is_in_cooldown(alert, now=t0 + timedelta(minutes=5))
    assert in_cd_5m is True
    assert "INC-TEST-1" in reason

    # 16 分後はクールダウン解除される
    in_cd_16m, _ = mgr.is_in_cooldown(alert, now=t0 + timedelta(minutes=16))
    assert in_cd_16m is False


def test_guardrail_downgrades_disallowed_namespace_and_circuit_breaker() -> None:
    """未許可 Namespace・低類似度・1時間1回上限超過がすべて REQUIRE_APPROVAL に安全降格されること。"""
    settings = AgentSettings(
        allowed_namespaces=("sre-demo-app",),
        max_auto_actions_per_hour=1,
        auto_remediate_similarity_threshold=0.85,
    )
    store = InMemoryEventStoreBackend()
    mgr = IncidentGuardrailManager(settings, store)
    t0 = datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)

    # 1. 未許可 Namespace (kube-system) は強制降格
    dec_ns, _, note_ns = mgr.enforce_safety_guardrails(
        namespace="kube-system",
        service_name="coredns",
        proposed_decision="AUTO_REMEDIATE",
        proposed_remediation="RESTART_DEPLOYMENT",
        similarity_score=0.98,
        now=t0,
    )
    assert dec_ns == "REQUIRE_APPROVAL"
    assert "kube-system" in note_ns

    # 2. 類似度不足 (0.72 < 0.85) は強制降格
    dec_sim, _, note_sim = mgr.enforce_safety_guardrails(
        namespace="sre-demo-app",
        service_name="order-service",
        proposed_decision="AUTO_REMEDIATE",
        proposed_remediation="ROLLBACK_DEPLOYMENT",
        similarity_score=0.72,
        now=t0,
    )
    assert dec_sim == "REQUIRE_APPROVAL"
    assert "0.72" in note_sim

    # 3. 条件を満たす初回は AUTO_REMEDIATE が通過
    dec_ok, rem_ok, note_ok = mgr.enforce_safety_guardrails(
        namespace="sre-demo-app",
        service_name="order-service",
        proposed_decision="AUTO_REMEDIATE",
        proposed_remediation="ROLLBACK_DEPLOYMENT",
        similarity_score=0.93,
        now=t0,
    )
    assert dec_ok == "AUTO_REMEDIATE"
    assert rem_ok == "ROLLBACK_DEPLOYMENT"
    assert note_ok == ""

    # 自動復旧を 1 回記録した後、30 分後の 2 回目はサーキットブレーカーで降格される
    alert = AlertEvent(incident_id="A-1", namespace="sre-demo-app", service_name="order-service", summary="bug")
    mgr.record_outcome(
        alert=alert,
        decision="AUTO_REMEDIATE",
        remediation_type="ROLLBACK_DEPLOYMENT",
        matched_runbook_id="INC-001",
        similarity_score=0.93,
        verification_passed=True,
        summary="rolled back",
        now=t0,
    )
    dec_cb, _, note_cb = mgr.enforce_safety_guardrails(
        namespace="sre-demo-app",
        service_name="order-service",
        proposed_decision="AUTO_REMEDIATE",
        proposed_remediation="ROLLBACK_DEPLOYMENT",
        similarity_score=0.95,
        now=t0 + timedelta(minutes=30),
    )
    assert dec_cb == "REQUIRE_APPROVAL"
    assert "上限" in note_cb


def test_pubsub_payload_normalization_and_target_app_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cloud Logging シンク形式のパースと監視対象 FastAPI アプリの 3 障害モードが正常動作すること。"""
    raw_log = b"""{
        "insertId": "log-991",
        "severity": "ERROR",
        "jsonPayload": {
            "namespace": "sre-demo-app",
            "service": "order-service",
            "error_code": "CONFIG_KEY_ERROR_500",
            "message": "KeyError: PAYMENT_CURRENCY_PRECISION",
            "introduced_in": "v2.0.0-buggy"
        }
    }"""
    parsed = parse_pubsub_payload(raw_log)
    assert parsed.incident_id == "log-991"
    assert parsed.error_code == "CONFIG_KEY_ERROR_500"
    assert parsed.recent_deployment_within_30m is True

    client = TestClient(target_app.app)
    monkeypatch.setenv("APP_VERSION", "v1.0.0")
    monkeypatch.setenv("PAYMENT_CURRENCY_PRECISION", "2")
    client.post("/admin/fault", json={"mode": "NONE", "duration_seconds": 0})

    ok_resp = client.post("/orders", json={"item_id": "SKU-1", "quantity": 1, "amount_jpy": 1000})
    assert ok_resp.status_code == 201

    client.post("/admin/fault", json={"mode": "TRANSIENT_NET_ERR", "duration_seconds": 60})
    net_resp = client.post("/orders", json={"item_id": "SKU-1", "quantity": 1, "amount_jpy": 1000})
    assert net_resp.status_code == 503

    client.post("/admin/fault", json={"mode": "DB_POOL_EXHAUSTED", "duration_seconds": 60})
    db_resp = client.post("/orders", json={"item_id": "SKU-1", "quantity": 1, "amount_jpy": 1000})
    assert db_resp.status_code == 500

    client.post("/admin/fault", json={"mode": "NONE", "duration_seconds": 0})
    monkeypatch.setenv("APP_VERSION", "v2.0.0-buggy")
    monkeypatch.delenv("PAYMENT_CURRENCY_PRECISION", raising=False)
    bug_resp = client.post("/orders", json={"item_id": "SKU-1", "quantity": 1, "amount_jpy": 1000})
    assert bug_resp.status_code == 500

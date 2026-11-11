"""Cloud Run sandboxes コマンド構築・統計解析および 100 件インシデント知識ベースのテスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_app.config import AgentSettings
from agent_app.mcp_tools import GKE_READONLY_ALLOWED_TOOLS, LOGGING_ALLOWED_TOOLS, MONITORING_ALLOWED_TOOLS
from agent_app.models import AlertEvent
from agent_app.sandbox_runner import SandboxRunner
from agent_app.vector_kb import LocalJsonlKnowledgeBase
from model_armor_guard import build_log_screening_plugin

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_sandbox_command_enforces_strict_isolation(tmp_path: Path) -> None:
    """Cloud Run sandbox バイナリ使用時にネットワーク遮断・読取専用・非対話フラグが強制されること。"""
    fake_sandbox_bin = tmp_path / "sandbox"
    fake_sandbox_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_sandbox_bin.chmod(0o755)

    settings = AgentSettings(sandbox_mode="cloud_run", sandbox_binary_path=str(fake_sandbox_bin))
    runner = SandboxRunner(settings)
    cmd = runner.build_command("print(1)", "[0.1, 0.2]", "sre-stat-test01")

    assert cmd[0] == str(fake_sandbox_bin)
    assert cmd[1] == "do"
    assert "--sandbox-name=sre-stat-test01" in cmd
    assert "--stdin=false" in cmd
    assert "--" in cmd
    # セキュリティ境界: 外向き通信許可・書き込み許可・環境変数転送フラグが一切含まれないこと
    assert "--allow-egress" not in cmd
    assert "--write" not in cmd
    assert "-e" not in cmd
    assert "--env" not in cmd

    # バイナリ不在時はサイレントにホスト実行へフォールバックせず Fail-Closed で検知すること
    missing_runner = SandboxRunner(
        AgentSettings(sandbox_mode="cloud_run", sandbox_binary_path=str(tmp_path / "missing-sandbox"))
    )
    with pytest.raises(RuntimeError, match="SANDBOX_MODE='cloud_run'"):
        missing_runner.build_command("print(1)", "[0.1]", "sre-stat-missing")


def test_sandbox_series_analysis_distinguishes_decay_from_persistent_failure() -> None:
    """時系列エラー率の線形回帰から一過性スパイクと持続的バグを正確に区別できること。"""
    runner = SandboxRunner(AgentSettings(sandbox_mode="local_subprocess"))

    # 一過性スパイク (5バケット): 52% まで跳ねた後に急速減衰して 2% へ回復
    transient = runner.analyze_error_series([0.48, 0.52, 0.21, 0.04, 0.02])
    assert transient.is_naturally_decaying is True
    assert transient.linear_slope < -0.05

    # 一過性スパイク (3バケット対称形: 正常 -> 急騰 -> 0% 完全回復) でも減衰区間から正しく判定できること
    three_bucket_spike = runner.analyze_error_series([0.0, 0.8889, 0.0])
    assert three_bucket_spike.is_naturally_decaying is True
    assert three_bucket_spike.linear_slope < -0.02

    # デプロイ起因の持続バグ: 0% から 90% へ上昇し高止まり
    persistent = runner.analyze_error_series([0.01, 0.82, 0.89, 0.91, 0.90])
    assert persistent.is_naturally_decaying is False
    assert persistent.linear_slope > 0.0


def test_mcp_tool_filter_and_model_armor_plugin_configuration() -> None:
    """公式リモート MCP の厳選 10 ツール定義と Model Armor 非同期プラグイン構成を検証する。"""
    total_tools = len(MONITORING_ALLOWED_TOOLS) + len(LOGGING_ALLOWED_TOOLS) + len(GKE_READONLY_ALLOWED_TOOLS)
    assert total_tools == 10
    assert "diagnose_k8s_workload" not in GKE_READONLY_ALLOWED_TOOLS
    assert "get_cluster" in GKE_READONLY_ALLOWED_TOOLS

    plugin = build_log_screening_plugin("demo-proj", "asia-northeast1", "sre-log-guard")
    # 同期 ModelArmorClient が注入されておらず、ADK 標準のリージョン別非同期クライアント遅延生成が使われること
    assert plugin._supplied_client is None
    assert plugin._location == "asia-northeast1"


def test_100_runbooks_dataset_integrity_and_retrieval() -> None:
    """100件の過去インシデント知識ベースから 3 つの代表障害シナリオが的確に検索されること。"""
    jsonl_path = REPO_ROOT / "bigquery" / "seed_runbooks_100.jsonl"
    kb = LocalJsonlKnowledgeBase(jsonl_path)

    # シナリオ 1: 決済ゲートウェイ TLS 瞬断 -> OBSERVE (INC-013)
    s1 = AlertEvent(
        incident_id="DRILL-1",
        error_code="UPSTREAM_CONN_RESET_503",
        summary="Upstream payment gateway connection reset by peer during TLS handshake",
        recent_deployment_within_30m=False,
    )
    m1 = kb.search_similar(s1, top_k=3)
    assert m1[0].runbook_id == "INC-013"
    assert m1[0].recommended_action == "OBSERVE"

    # シナリオ 2: デプロイ直後の必須環境変数 KeyError -> AUTO_REMEDIATE (INC-001)
    s2 = AlertEvent(
        incident_id="DRILL-2",
        error_code="CONFIG_KEY_ERROR_500",
        summary="KeyError: PAYMENT_CURRENCY_PRECISION missing after deployment rollout",
        recent_deployment_within_30m=True,
    )
    m2 = kb.search_similar(s2, top_k=3)
    assert m2[0].runbook_id == "INC-001"
    assert m2[0].recommended_action == "AUTO_REMEDIATE"
    assert m2[0].remediation_type == "ROLLBACK_DEPLOYMENT"

    # シナリオ 3: DB コネクションスロット枯渇・ロック待ち -> REQUIRE_APPROVAL (INC-025)
    s3 = AlertEvent(
        incident_id="DRILL-3",
        error_code="DB_CONN_SLOTS_EXHAUSTED_500",
        summary="OperationalError: FATAL: remaining connection slots are reserved for superuser; active lock wait",
        recent_deployment_within_30m=False,
    )
    m3 = kb.search_similar(s3, top_k=3)
    assert m3[0].runbook_id == "INC-025"
    assert m3[0].recommended_action == "REQUIRE_APPROVAL"

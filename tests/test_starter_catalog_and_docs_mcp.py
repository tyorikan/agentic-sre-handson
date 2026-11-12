"""汎用障害パターン集・手順書の取り込み・公式ドキュメント検索 MCP（補助）のテスト。"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType

import pytest
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

from agent_app.config import AgentSettings
from agent_app.email_notifier import build_approval_request_email
from agent_app.mcp_tools import (
    DEVELOPER_KNOWLEDGE_ALLOWED_TOOLS,
    DEVELOPER_KNOWLEDGE_MCP_URL,
    DEVELOPER_KNOWLEDGE_TIMEOUT_SECONDS,
    OptionalMcpToolset,
    build_observability_mcp_toolsets,
)
from agent_app.models import AlertEvent, TriageDecision
from agent_app.slack_socket import build_approval_request_blocks
from agent_app.vector_kb import LocalJsonlKnowledgeBase
from agent_app.workflow import WorkflowRunResult

REPO_ROOT = Path(__file__).resolve().parents[1]
BIGQUERY_DIR = REPO_ROOT / "bigquery"
STARTER_CATALOG = BIGQUERY_DIR / "seed_runbooks_production_starter.jsonl"
HANDSON_SEED = BIGQUERY_DIR / "seed_runbooks_100.jsonl"

STARTER_CATEGORIES = {
    "Kubernetes ワークロード・Pod ライフサイクル",
    "GKE ノード・オートスケール・アップグレード",
    "デプロイ・設定・アプリランタイム",
    "データベース・キャッシュ",
    "ネットワーク・ロードバランサ・DNS",
    "認証・IAM・Secret・証明書・割り当て",
    "メッセージング・非同期処理",
    "外部依存・トラフィック変動・ログに出にくい障害",
}
EXECUTABLE_TYPES = ("ROLLBACK_DEPLOYMENT", "RESTART_DEPLOYMENT")


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_import_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "import_markdown_runbooks", BIGQUERY_DIR / "import_markdown_runbooks.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 汎用障害パターン集（本番・PoC 導入用の初期データ）
# ---------------------------------------------------------------------------


def test_starter_catalog_never_allows_unattended_changes() -> None:
    """汎用の初期データは、変更を伴う対応をすべて人の承認待ちにし、項目の形式がハンズオン用データと同じであること。"""
    rows = _load_jsonl(STARTER_CATALOG)
    expected_keys = list(_load_jsonl(HANDSON_SEED)[0].keys())

    assert 200 <= len(rows) <= 300
    assert len({r["runbook_id"] for r in rows}) == len(rows)

    for row in rows:
        assert list(row.keys()) == expected_keys, row["runbook_id"]
        assert re.fullmatch(r"GEN-\d{3}", row["runbook_id"]), row["runbook_id"]
        assert row["category"] in STARTER_CATEGORIES, row["runbook_id"]
        if row["recommended_action"] == "OBSERVE":
            assert row["remediation_type"] == "NONE", row["runbook_id"]
        else:
            assert row["recommended_action"] == "REQUIRE_APPROVAL", row["runbook_id"]
            assert row["remediation_type"] in (*EXECUTABLE_TYPES, "MANUAL_INTERVENTION"), row["runbook_id"]
        # 実績値が無いため、復旧時間と検証経過日数は 0 に固定する
        assert row["historical_mttr_minutes"] == 0, row["runbook_id"]
        assert row["verified_age_days"] == 0, row["runbook_id"]
        for key in expected_keys[1:13]:
            assert isinstance(row[key], str) and row[key].strip(), (row["runbook_id"], key)


def test_starter_catalog_contains_no_handson_specific_names() -> None:
    """ハンズオン用データにある架空のサービス名や教材固有のエラーコードが、汎用の初期データに混ざっていないこと。"""
    handson_names: set[str] = set()
    for row in _load_jsonl(HANDSON_SEED):
        handson_names.update(re.findall(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+", row["service_pattern"]))
    handson_names.update(
        {
            "CONFIG_KEY_ERROR_500",
            "DB_CONN_SLOTS_EXHAUSTED_500",
            "UPSTREAM_CONN_RESET_503",
            "PAYMENT_CURRENCY_PRECISION",
            "TAX_RULE_CRC32_MISMATCH_500",
        }
    )
    pattern = re.compile(
        r"(?<![A-Za-z0-9_-])(?:" + "|".join(sorted(map(re.escape, handson_names))) + r")(?![A-Za-z0-9_-])"
    )

    for row in _load_jsonl(STARTER_CATALOG):
        text = " ".join(str(v) for v in row.values())
        assert not pattern.search(text), (row["runbook_id"], pattern.search(text).group(0))


def test_starter_catalog_is_searchable_with_the_local_knowledge_base() -> None:
    """汎用の初期データを、ローカル検証用の知識ベースでそのまま読み込めること。"""
    kb = LocalJsonlKnowledgeBase(STARTER_CATALOG)
    assert kb.total_records == len(_load_jsonl(STARTER_CATALOG))


def test_starter_seed_sql_registers_rows_as_unverified() -> None:
    """投入 SQL が、判断区分を承認待ちへそろえ、未検証（最終検証日なし）として登録すること。"""
    sql = (BIGQUERY_DIR / "03_seed_production_starter.sql").read_text(encoding="utf-8")
    handson_sql = (BIGQUERY_DIR / "02_seed_and_verify_ai_search.sql").read_text(encoding="utf-8")
    statements = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    merge_statement = statements.split(";")[0]

    assert "IF(recommended_action = 'OBSERVE', 'OBSERVE', 'REQUIRE_APPROVAL')" in merge_statement
    assert "AUTO_REMEDIATE" not in merge_statement
    # created_at と last_verified_at を NULL にすると、検索側で「最終検証日なし」（鮮度切れ）として扱われる
    assert merge_statement.count("CAST(NULL AS TIMESTAMP)") == 2

    def search_document_expression(text: str) -> str:
        match = re.search(r"CONCAT\((.*?)\)\s+AS search_document", text, re.S)
        assert match is not None
        return re.sub(r"\s+", " ", match.group(1)).strip()

    # 埋め込みの対象になる検索用テキストは、ハンズオン用の投入 SQL と同じ組み立てにする
    assert search_document_expression(sql) == search_document_expression(handson_sql)


@pytest.mark.parametrize(
    ("age_days", "expected", "unexpected"),
    [
        (9999.0, "この環境では未検証", "9999"),
        (12.0, "最終検証", "未検証"),
    ],
)
def test_approval_notifications_say_when_a_match_was_never_verified_here(
    age_days: float, expected: str, unexpected: str
) -> None:
    """未検証として登録した事例に一致したとき、承認の通知が「最終検証 9999日前」ではなく未検証と伝えること。"""
    alert = AlertEvent(incident_id="GEN-NOTICE-1", error_code="HTTP_500", summary="KeyError: 'REQUIRED_SETTING'")
    decision = TriageDecision(
        decision="REQUIRE_APPROVAL",
        remediation_type="ROLLBACK_DEPLOYMENT",
        target_namespace="sre-demo-app",
        target_deployment="order-service",
        matched_runbook_id="GEN-081",
        similarity_score=0.87,
        matched_runbook_age_days=age_days,
        root_cause_hypothesis="必須の環境変数が設定されていないと推定されます。",
        rationale="既知のパターンに一致するため、ロールバックの承認を求めます。",
    )
    result = WorkflowRunResult(
        alert=alert,
        session_id="s1",
        invocation_id="i1",
        decision=decision,
        paused_for_approval=True,
        outcome=None,
    )

    slack_text = json.dumps(build_approval_request_blocks(result), ensure_ascii=False)
    mail_body = build_approval_request_email(alert, decision).body

    for rendered in (slack_text, mail_body):
        assert "GEN-081" in rendered
        assert expected in rendered
        assert unexpected not in rendered


# ---------------------------------------------------------------------------
# 既存の手順書の取り込み
# ---------------------------------------------------------------------------


def test_runbook_import_offline_is_always_approval_gated(tmp_path: Path) -> None:
    """API を呼ばない変換では、本文に「ロールバック」とあっても操作種別を推測せず、承認待ち・人手対応として出力すること。"""
    importer = _load_import_script()
    source_dir = tmp_path / "runbooks"
    source_dir.mkdir()
    (source_dir / "db_failover.md").write_text(
        "# フェイルオーバー直後のデータベース接続エラー\n\n"
        "## 症状\n- FATAL: the database system is in recovery mode\n\n"
        "## 原因\n- フェイルオーバーの直後に、切り替え前の接続が残っている\n\n"
        "## 対処\n- アプリを前のバージョンへロールバックしてはいけない。接続プールを再作成する\n\n"
        "## 確認\n- 接続エラーが出なくなる\n",
        encoding="utf-8",
    )
    output = tmp_path / "out.jsonl"

    exit_code = importer.main(["--input", str(source_dir), "--output", str(output), "--offline"])

    assert exit_code == 0
    rows = _load_jsonl(output)
    assert len(rows) == 1
    row = rows[0]
    assert list(row.keys()) == list(_load_jsonl(HANDSON_SEED)[0].keys())
    assert row["runbook_id"] == importer.build_runbook_id("CUST", "db_failover.md")
    assert row["recommended_action"] == "REQUIRE_APPROVAL"
    assert row["remediation_type"] == "MANUAL_INTERVENTION"
    assert row["historical_mttr_minutes"] == 0
    assert "recovery mode" in row["symptom_signature"]
    # 手順書に無い項目は、空のままにせず、追記が必要なことが分かる文言を入れる
    assert row["metrics_pattern"] == importer.NOT_DOCUMENTED


def test_runbook_import_overrides_whatever_the_model_extracted() -> None:
    """抽出結果の操作種別が想定外でも、承認後に実行できる 2 種類以外は人手対応へ寄せること。"""
    importer = _load_import_script()
    base = importer.ExtractedRunbookRecord(
        category="データベース・キャッシュ",
        title="件名",
        service_pattern="GKE Deployment",
        symptom_signature="error",
        metrics_pattern="指標",
        recent_change_hint="変更",
        root_cause="原因",
        remediation_type="NONE",
        remediation_steps="手順",
        anti_patterns="禁止事項",
        verification_check="確認",
    )
    expectations = {
        "NONE": "MANUAL_INTERVENTION",
        "DELETE_NAMESPACE": "MANUAL_INTERVENTION",
        "AUTO_REMEDIATE": "MANUAL_INTERVENTION",
        "rollback_deployment": "ROLLBACK_DEPLOYMENT",
        "RESTART_DEPLOYMENT": "RESTART_DEPLOYMENT",
    }

    for extracted, expected in expectations.items():
        record = importer.to_safe_record(base.model_copy(update={"remediation_type": extracted}), "CUST-TEST")
        assert record["recommended_action"] == "REQUIRE_APPROVAL"
        assert record["remediation_type"] == expected
        assert record["historical_mttr_minutes"] == 0


def test_runbook_import_reports_files_it_could_not_convert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """抽出に失敗したファイルがあっても残りは変換し、失敗があったことを終了コードで知らせること。"""
    importer = _load_import_script()
    source_dir = tmp_path / "runbooks"
    source_dir.mkdir()
    (source_dir / "a_ok.md").write_text("# 変換できる手順書\n\n本文", encoding="utf-8")
    (source_dir / "b_fail.md").write_text("# 変換に失敗する手順書\n\n本文", encoding="utf-8")
    output = tmp_path / "out.jsonl"

    def fake_extract(md_text: str, fallback_title: str, model_name: str):
        if "失敗する" in md_text:
            raise RuntimeError("モデルから応答本文が返りませんでした。")
        return importer.parse_markdown_offline(md_text, fallback_title)

    monkeypatch.setattr(importer, "extract_with_gemini", fake_extract)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *_args, **_kwargs: False)

    exit_code = importer.main(["--input", str(source_dir), "--output", str(output)])

    assert exit_code == 2
    rows = _load_jsonl(output)
    assert [row["title"] for row in rows] == ["変換できる手順書"]


# ---------------------------------------------------------------------------
# 公式ドキュメント検索 MCP（補助）
# ---------------------------------------------------------------------------


def test_developer_knowledge_mcp_is_enabled_by_default_and_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENABLE_DEVELOPER_KNOWLEDGE_MCP", raising=False)
    assert AgentSettings().enable_developer_knowledge_mcp is True

    monkeypatch.setenv("ENABLE_DEVELOPER_KNOWLEDGE_MCP", "false")
    assert AgentSettings().enable_developer_knowledge_mcp is False


def test_developer_knowledge_toolset_is_optional_read_only_and_time_boxed() -> None:
    """ドキュメント検索は、設定で外せて、検索と取得の 2 ツールだけを公開し、待ち時間を短く区切ること。"""
    enabled = build_observability_mcp_toolsets(AgentSettings(enable_developer_knowledge_mcp=True))
    disabled = build_observability_mcp_toolsets(AgentSettings(enable_developer_knowledge_mcp=False))

    assert len(disabled) == 3
    assert not any(isinstance(toolset, OptionalMcpToolset) for toolset in disabled)

    assert len(enabled) == 4
    docs_toolset = enabled[-1]
    assert isinstance(docs_toolset, OptionalMcpToolset)
    assert docs_toolset.tool_filter == DEVELOPER_KNOWLEDGE_ALLOWED_TOOLS == ["search_documents", "get_documents"]
    params = docs_toolset._connection_params
    assert params.url == DEVELOPER_KNOWLEDGE_MCP_URL
    assert params.timeout == DEVELOPER_KNOWLEDGE_TIMEOUT_SECONDS
    assert params.sse_read_timeout == DEVELOPER_KNOWLEDGE_TIMEOUT_SECONDS


async def test_optional_mcp_toolset_continues_without_tools_when_listing_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """補助用途の接続先からツール一覧を取得できなくても、例外を上げず、警告ログを残してツールなしで続けること。"""

    async def failing_get_tools(self, readonly_context=None):
        raise ConnectionError("Failed to create MCP session: Not Found")

    monkeypatch.setattr(McpToolset, "get_tools", failing_get_tools)
    toolset = OptionalMcpToolset(
        connection_params=StreamableHTTPConnectionParams(url="https://mcp.invalid/mcp"),
    )

    assert await toolset.get_tools() == []

    log_line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert log_line["severity"] == "WARNING"
    assert log_line["event"] == "optional_mcp_unavailable"
    assert log_line["mcp_url"] == "https://mcp.invalid/mcp"
    assert "ConnectionError" in log_line["error"]

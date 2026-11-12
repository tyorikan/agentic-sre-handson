"""Google Cloud 公式リモート MCP サーバー接続モジュール（Phase 1 読み取り調査専用）。

対象 API を有効化し、エージェントのサービスアカウントへ `roles/mcp.toolUser` と
`roles/serviceusage.serviceUsageConsumer` を付与して使う Google 提供のリモート MCP エンドポイントへ、
Streamable HTTP で接続します。

接続先は 2 種類に分かれます。

1. 環境の観測（必須）: Monitoring / Logging / GKE の 3 エンドポイント・計 10 ツール。
   GKE は読み取り専用のエンドポイント (`/mcp/read-only`) だけを使い、MCP 経由のクラスタ変更を構造的に遮断します。
2. 公式ドキュメントの検索（補助）: Developer Knowledge の 1 エンドポイント・計 2 ツール。
   初見のエラーについて、Google Cloud の公開ドキュメントを検索します。
   補助用途のため、一覧を取得できない場合はツールなしで調査を続けます（`OptionalMcpToolset`）。

どちらも `tool_filter` で公開するツールを絞り、モデルが選ぶ候補とトークン消費を抑えます。
"""

from __future__ import annotations

import json
import sys
from typing import Any

import google.auth
import google.auth.transport.requests
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

from .config import AgentSettings

MONITORING_MCP_URL = "https://monitoring.googleapis.com/mcp"
LOGGING_MCP_URL = "https://logging.googleapis.com/mcp"
GKE_READONLY_MCP_URL = "https://container.googleapis.com/mcp/read-only"
DEVELOPER_KNOWLEDGE_MCP_URL = "https://developerknowledge.googleapis.com/mcp"

# 各 MCP サーバーから公開を許可する読み取り専用ツールの厳選リスト
MONITORING_ALLOWED_TOOLS = [
    "list_timeseries",
    "query_range",
    "list_alerts",
]

LOGGING_ALLOWED_TOOLS = [
    "list_log_entries",
]

GKE_READONLY_ALLOWED_TOOLS = [
    "get_cluster",
    "get_k8s_resource",
    "describe_k8s_resource",
    "list_k8s_events",
    "get_k8s_rollout_status",
    "get_k8s_logs",
]

# 回答を生成する answer_query は 1 日あたりの割り当てが小さいため公開しない
DEVELOPER_KNOWLEDGE_ALLOWED_TOOLS = [
    "search_documents",
    "get_documents",
]

# 補助用途の接続で待つ上限（秒）。接続の確立と応答の読み取りの両方に使う。
# 応答が返らない場合でも、トリアージ全体の制限時間を使い切らないよう短くする
DEVELOPER_KNOWLEDGE_TIMEOUT_SECONDS = 10.0


class OptionalMcpToolset(McpToolset):
    """補助用途の MCP サーバー向けのツールセット。

    ツール一覧を取得できない場合（API が無効・権限不足・到達不能など）は、警告ログを 1 行出して
    ツールなしとして扱います。

    ADK 2.14 は、読み込みに失敗したツールセットを除いて実行を続けますが、これはフレームワーク内部の挙動で、
    失敗のたびに ERROR ログとスタックトレースを出します。補助用途の接続先では、続行することをこのクラスで明示し、
    ログも WARNING 1 行にとどめます。
    """

    async def get_tools(self, readonly_context: Any = None) -> list[Any]:
        try:
            return await super().get_tools(readonly_context)
        except Exception as exc:  # noqa: BLE001
            payload = {
                "severity": "WARNING",
                "message": "補助用途の MCP サーバーのツール一覧を取得できないため、このツールなしで調査を続けます。",
                "event": "optional_mcp_unavailable",
                "mcp_url": getattr(self._connection_params, "url", ""),
                "error": f"{type(exc).__name__}: {exc}"[:300],
            }
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            return []


def _build_google_auth_header_provider(project_id: str):
    """Application Default Credentials (ADC) をキャッシュ保持し、有効期限切れ時のみ更新してヘッダを返す。"""
    cached_creds: list[Any] = []
    auth_req = google.auth.transport.requests.Request()

    def _provider(_readonly_context: Any = None) -> dict[str, str]:
        if not cached_creds:
            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
            cached_creds.append(creds)
        credentials = cached_creds[0]
        if not credentials.valid or not credentials.token:
            credentials.refresh(auth_req)
        return {
            "Authorization": f"Bearer {credentials.token}",
            "x-goog-user-project": project_id,
        }

    return _provider


def build_observability_mcp_toolsets(settings: AgentSettings) -> list[McpToolset]:
    """Monitoring / Logging / GKE (Read-Only) と、有効な場合は Developer Knowledge のツールセット群を生成する。"""
    header_provider = _build_google_auth_header_provider(settings.project_id)

    monitoring_toolset = McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=MONITORING_MCP_URL, timeout=15.0),
        tool_filter=MONITORING_ALLOWED_TOOLS,
        tool_name_prefix="monitoring_",
        tool_list_cache_ttl_seconds=300.0,
        header_provider=header_provider,
    )
    logging_toolset = McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=LOGGING_MCP_URL, timeout=15.0),
        tool_filter=LOGGING_ALLOWED_TOOLS,
        tool_name_prefix="logging_",
        tool_list_cache_ttl_seconds=300.0,
        header_provider=header_provider,
    )
    gke_readonly_toolset = McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=GKE_READONLY_MCP_URL, timeout=15.0),
        tool_filter=GKE_READONLY_ALLOWED_TOOLS,
        tool_name_prefix="gke_",
        tool_list_cache_ttl_seconds=300.0,
        header_provider=header_provider,
    )
    toolsets: list[McpToolset] = [monitoring_toolset, logging_toolset, gke_readonly_toolset]

    if settings.enable_developer_knowledge_mcp:
        toolsets.append(
            OptionalMcpToolset(
                connection_params=StreamableHTTPConnectionParams(
                    url=DEVELOPER_KNOWLEDGE_MCP_URL,
                    timeout=DEVELOPER_KNOWLEDGE_TIMEOUT_SECONDS,
                    # 既定の読み取り待ちは 5 分。補助用途の検索では待たせない
                    sse_read_timeout=DEVELOPER_KNOWLEDGE_TIMEOUT_SECONDS,
                ),
                tool_filter=DEVELOPER_KNOWLEDGE_ALLOWED_TOOLS,
                tool_name_prefix="devknowledge_",
                tool_list_cache_ttl_seconds=300.0,
                header_provider=header_provider,
            )
        )
    return toolsets

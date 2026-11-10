"""Google Cloud 公式リモート MCP サーバー接続モジュール（Phase 1 読み取り調査専用）。

対象 API を有効化し `roles/mcp.toolUser` を付与するだけで利用できる Google Cloud マネージド
MCP エンドポイントへ Streamable HTTP で接続します。

セキュリティとトークン効率の設計:
  - GKE は必ず読み取り専用ツールセット (`https://container.googleapis.com/mcp/read-only`) を使用し、
    MCP 経由でのクラスタ変更・削除を構造的に遮断します。
  - `tool_filter` により、20 個以上の汎用ツールの中から障害相関調査に必要な最小限の
    読み取りツールのみをモデルへ公開し、推論迷走とトークン消費を抑えます。
"""

from __future__ import annotations

from typing import Any

import google.auth
import google.auth.transport.requests
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

from .config import AgentSettings

MONITORING_MCP_URL = "https://monitoring.googleapis.com/mcp"
LOGGING_MCP_URL = "https://logging.googleapis.com/mcp"
GKE_READONLY_MCP_URL = "https://container.googleapis.com/mcp/read-only"

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
    "get_k8s_resource",
    "describe_k8s_resource",
    "list_k8s_events",
    "get_k8s_rollout_status",
]


def _build_google_auth_header_provider(project_id: str):
    """Application Default Credentials (ADC) から最新の OAuth 2.0 トークンを取得してヘッダを返す。"""

    def _provider(_readonly_context: Any = None) -> dict[str, str]:
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not credentials.valid:
            credentials.refresh(google.auth.transport.requests.Request())
        return {
            "Authorization": f"Bearer {credentials.token}",
            "x-goog-user-project": project_id,
        }

    return _provider


def build_observability_mcp_toolsets(settings: AgentSettings) -> list[McpToolset]:
    """Monitoring / Logging / GKE (Read-Only) の厳選済みリモート MCP ツールセット群を生成する。"""
    header_provider = _build_google_auth_header_provider(settings.project_id)

    monitoring_toolset = McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=MONITORING_MCP_URL, timeout=15.0),
        tool_filter=MONITORING_ALLOWED_TOOLS,
        tool_name_prefix="monitoring_",
        header_provider=header_provider,
    )
    logging_toolset = McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=LOGGING_MCP_URL, timeout=15.0),
        tool_filter=LOGGING_ALLOWED_TOOLS,
        tool_name_prefix="logging_",
        header_provider=header_provider,
    )
    gke_readonly_toolset = McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=GKE_READONLY_MCP_URL, timeout=15.0),
        tool_filter=GKE_READONLY_ALLOWED_TOOLS,
        tool_name_prefix="gke_",
        header_provider=header_provider,
    )
    return [monitoring_toolset, logging_toolset, gke_readonly_toolset]

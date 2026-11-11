"""環境変数に基づく設定管理モジュール。

モデル名、リージョン、許可 Namespace、クールダウン時間、サーキットブレーカー上限、
および自律ナレッジ蓄積フラグをすべて環境変数から読み込み、コードへのハードコードを排除します。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv


def _split_csv(raw: str) -> tuple[str, ...]:
    """カンマ区切り文字列を空白除去済みタプルへ変換する。"""
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _parse_bool(raw: str) -> bool:
    """環境変数文字列を真偽値へ変換する。"""
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class AgentSettings:
    """常駐 SRE エージェントの実行時設定。"""

    project_id: str = field(default_factory=lambda: os.getenv("GOOGLE_CLOUD_PROJECT", "demo-project"))
    region: str = field(default_factory=lambda: os.getenv("GOOGLE_CLOUD_REGION", "asia-northeast1"))
    genai_location: str = field(default_factory=lambda: os.getenv("GOOGLE_CLOUD_LOCATION", "global"))

    # Gemini モデル設定（既定は現行 GA の gemini-3.8-flash / gemini-embedding-2）
    triage_model: str = field(default_factory=lambda: os.getenv("SRE_TRIAGE_MODEL", "gemini-3.8-flash"))
    embedding_model: str = field(default_factory=lambda: os.getenv("SRE_EMBEDDING_MODEL", "gemini-embedding-2"))

    # Pub/Sub 常時購読設定
    alert_subscription: str = field(default_factory=lambda: os.getenv("ALERT_PUBSUB_SUBSCRIPTION", "sre-alert-stream-sub"))

    # BigQuery 知識ベース（Autonomous Embedding Generation）& 状態管理設定
    bq_dataset: str = field(default_factory=lambda: os.getenv("BQ_DATASET", "sre_agent_ops"))
    bq_runbooks_table: str = field(default_factory=lambda: os.getenv("BQ_RUNBOOKS_TABLE", "incident_runbooks"))
    bq_events_table: str = field(default_factory=lambda: os.getenv("BQ_EVENTS_TABLE", "incident_events"))
    bq_connection_id: str = field(default_factory=lambda: os.getenv("BQ_CONNECTION_ID", "sre_genai_conn"))
    enable_autonomous_kb_learning: bool = field(
        default_factory=lambda: _parse_bool(os.getenv("ENABLE_AUTONOMOUS_KB_LEARNING", "true"))
    )
    runbook_freshness_ttl_days: int = field(
        default_factory=lambda: int(os.getenv("RUNBOOK_FRESHNESS_TTL_DAYS", "90"))
    )

    # GKE クラスタと復旧操作の安全境界
    gke_cluster_name: str = field(default_factory=lambda: os.getenv("GKE_CLUSTER_NAME", "sre-demo-cluster"))
    gke_cluster_location: str = field(default_factory=lambda: os.getenv("GKE_CLUSTER_LOCATION", "asia-northeast1"))
    allowed_namespaces: tuple[str, ...] = field(
        default_factory=lambda: _split_csv(os.getenv("ALLOWED_K8S_NAMESPACES", "sre-demo-app"))
    )
    cooldown_minutes: int = field(default_factory=lambda: int(os.getenv("COOLDOWN_MINUTES", "15")))
    max_auto_actions_per_hour: int = field(default_factory=lambda: int(os.getenv("MAX_AUTO_ACTIONS_PER_HOUR", "1")))
    auto_remediate_similarity_threshold: float = field(
        default_factory=lambda: float(os.getenv("AUTO_REMEDIATE_SIMILARITY_THRESHOLD", "0.85"))
    )
    allow_drill_cooldown_reset: bool = field(
        default_factory=lambda: _parse_bool(os.getenv("ALLOW_DRILL_COOLDOWN_RESET", "false"))
    )

    # サンドボックス・観測ウィンドウ設定 ("cloud_run" または "local_subprocess")
    sandbox_mode: str = field(default_factory=lambda: os.getenv("SANDBOX_MODE", "cloud_run"))
    sandbox_binary_path: str = field(
        default_factory=lambda: os.getenv("SANDBOX_BINARY_PATH", "/usr/local/gcp/bin/sandbox")
    )
    stabilization_wait_seconds: int = field(
        default_factory=lambda: int(os.getenv("INITIAL_STABILIZATION_SECONDS", "0"))
    )

    # Cloud Monitoring SLO モニタリング & Error Budget 温存効果算出設定
    slo_service_id: str = field(default_factory=lambda: os.getenv("SLO_SERVICE_ID", "order-service-slo"))
    slo_daily_id: str = field(default_factory=lambda: os.getenv("SLO_DAILY_ID", "order-availability-daily-99"))
    slo_monthly_id: str = field(default_factory=lambda: os.getenv("SLO_MONTHLY_ID", "order-availability-monthly-999"))
    manual_baseline_mttr_minutes: float = field(
        default_factory=lambda: float(os.getenv("MANUAL_BASELINE_MTTR_MINUTES", "30.0"))
    )

    # Slack Socket Mode 設定
    slack_bot_token: str = field(default_factory=lambda: os.getenv("SLACK_BOT_TOKEN", ""))
    slack_app_token: str = field(default_factory=lambda: os.getenv("SLACK_APP_TOKEN", ""))
    slack_channel_id: str = field(default_factory=lambda: os.getenv("SLACK_ALERT_CHANNEL_ID", ""))
    slack_allowed_approvers: tuple[str, ...] = field(
        default_factory=lambda: _split_csv(os.getenv("SLACK_ALLOWED_APPROVER_IDS", ""))
    )

    # ADK セッション・承認待ちチケット永続化（Human-in-the-Loop 一時停止と再開で使用）
    # 本番は "firestore"（インスタンス再起動耐性）、ローカル開発・単体テスト時は "sqlite" を選択
    session_backend: str = field(default_factory=lambda: os.getenv("SESSION_BACKEND", "sqlite"))
    firestore_database: str = field(default_factory=lambda: os.getenv("FIRESTORE_DATABASE", "(default)"))
    firestore_sessions_collection: str = field(
        default_factory=lambda: os.getenv("FIRESTORE_SESSIONS_COLLECTION", "adk-session")
    )
    firestore_tickets_collection: str = field(
        default_factory=lambda: os.getenv("FIRESTORE_TICKETS_COLLECTION", "sre_pending_approval_tickets")
    )
    session_db_url: str = field(
        default_factory=lambda: os.getenv("SESSION_DB_URL", "sqlite+aiosqlite:////tmp/sre_agent_sessions.db")
    )


def load_settings() -> AgentSettings:
    """`.env`（存在する場合）と最新の環境変数から設定オブジェクトを構築する。"""
    load_dotenv()
    return AgentSettings()

"""常駐 SRE エージェントのドメインモデル定義。

入力アラート、隔離サンドボックス分析結果、ベクトル検索結果、3 分岐の意思決定スキーマ、
自律学習用 Runbook 合成スキーマ、および Slack 通知用インシデントレポートを型安全に表現します。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, Field

DecisionType = Literal["OBSERVE", "AUTO_REMEDIATE", "REQUIRE_APPROVAL"]
RemediationType = Literal["NONE", "ROLLBACK_DEPLOYMENT", "RESTART_DEPLOYMENT", "MANUAL_INTERVENTION"]


class AlertEvent(BaseModel):
    """Pub/Sub（Cloud Monitoring アラートまたは Cloud Logging シンク）から受信した正規化イベント。"""

    incident_id: str = Field(description="インシデント追跡 ID")
    namespace: str = Field(default="sre-demo-app", description="対象 Kubernetes Namespace")
    service_name: str = Field(default="order-service", description="対象 Deployment / サービス名")
    error_code: str = Field(default="UNKNOWN_ERROR", description="代表エラーコードまたはポリシー名")
    severity: str = Field(default="ERROR", description="重大度 (WARNING / ERROR / CRITICAL)")
    summary: str = Field(description="アラートまたはエラーログの要約メッセージ")
    error_rate_series: list[float] = Field(
        default_factory=list,
        description="直近数分間の時系列エラー率サンプル（例: [0.02, 0.45, 0.30, 0.05]）",
    )
    recent_deployment_within_30m: bool = Field(
        default=False,
        description="直近 30 分以内に Deployment のロールアウト変更があったか",
    )
    observed_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def compute_fingerprint(self) -> str:
        """同一障害の重複排除・クールダウン判定に用いる決定論的ハッシュ値を算出する。"""
        raw = f"{self.namespace}:{self.service_name}:{self.error_code}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class SandboxMetricsAnalysis(BaseModel):
    """隔離サンドボックス内で実行した時系列統計解析の結果。"""

    sample_count: int
    peak_error_rate: float
    latest_error_rate: float
    linear_slope: float = Field(description="時系列エラー率の線形回帰傾き（負なら減衰傾向、正または0付近なら継続中）")
    is_naturally_decaying: bool = Field(description="一過性スパイクとして自然減衰中と判定できるか")


class SimilarRunbookMatch(BaseModel):
    """BigQuery AI.SEARCH から返却された過去の類似インシデント情報。"""

    runbook_id: str
    category: str
    title: str
    root_cause: str
    recommended_action: DecisionType
    remediation_type: RemediationType
    remediation_steps: str
    anti_patterns: str
    verification_check: str
    similarity_score: float = Field(ge=0.0, le=1.0)


class TriageDecision(BaseModel):
    """LLM 分析ノードが出力し、さらに決定論的ガードレールで検証される判断結果。"""

    decision: DecisionType = Field(description="OBSERVE | AUTO_REMEDIATE | REQUIRE_APPROVAL のいずれか")
    remediation_type: RemediationType = Field(description="実行または提案する復旧操作")
    target_namespace: str = Field(description="対象 Namespace")
    target_deployment: str = Field(description="対象 Deployment 名")
    matched_runbook_id: str = Field(default="NONE", description="根拠とした過去インシデント ID")
    similarity_score: float = Field(default=0.0, description="過去インシデントとのコサイン類似度")
    root_cause_hypothesis: str = Field(description="相関分析から導かれた根本原因の推定")
    rationale: str = Field(description="なぜ静観・自動復旧・人間承認のいずれを選んだかの明確な根拠")
    anti_pattern_warning: str = Field(default="", description="避けるべき誤対応の注意喚起")


class SynthesizedRunbookDraft(BaseModel):
    """Gemini が承認解決済みインシデントから抽出する構造化 Runbook ドラフト。"""

    category: str = Field(description="障害カテゴリ名")
    title: str = Field(description="簡潔で識別しやすいインシデント件名")
    service_pattern: str = Field(description="発生しやすいサービス構成・コンポーネント特徴")
    metrics_pattern: str = Field(description="エラー率やレイテンシの時系列推移パターン")
    recent_change_hint: str = Field(description="直近デプロイや設定変更との関連性")
    root_cause: str = Field(description="特定された根本原因")
    remediation_steps: str = Field(description="実証された復旧手順または手動対応手順")
    anti_patterns: str = Field(description="実行してはいけない誤対応とその理由")
    verification_check: str = Field(description="処置後に正常回復したと判定する確認基準")


class SynthesizedRunbookEntry(BaseModel):
    """BigQuery incident_runbooks テーブル（AI.EMBED 自動埋め込み列付き）へ登録する確定レコード。"""

    runbook_id: str
    category: str
    title: str
    service_pattern: str
    symptom_signature: str
    metrics_pattern: str
    recent_change_hint: str
    root_cause: str
    recommended_action: DecisionType
    remediation_type: RemediationType
    remediation_steps: str
    anti_patterns: str
    verification_check: str
    historical_mttr_minutes: int = 5

    def build_search_document(self) -> str:
        """初期シード 100 件と同一フォーマットの AI.EMBED 対象テキストを組み立てる。"""
        return (
            f"カテゴリ: {self.category}\n"
            f"件名: {self.title}\n"
            f"対象構成: {self.service_pattern}\n"
            f"エラー兆候: {self.symptom_signature}\n"
            f"メトリクス推移: {self.metrics_pattern}\n"
            f"変更相関: {self.recent_change_hint}\n"
            f"根本原因: {self.root_cause}"
        )


class RemediationOutcome(BaseModel):
    """復旧アクション実行（または静観）および事後メトリクス確認・自律ナレッジ登録の最終結果。"""

    executed: bool
    action_status: str = Field(description="OBSERVED | ROLLED_BACK | RESTARTED | AWAITING_APPROVAL | REJECTED | FAILED")
    detail_message: str
    verification_passed: bool = False
    post_error_rate: float | None = None
    learned_runbook_id: str | None = Field(
        default=None,
        description="承認解決後に incident_runbooks へ新規自律登録された Runbook ID（例: AUTO-A1B2C3D4）",
    )
    learned_recommended_action: DecisionType | None = Field(
        default=None,
        description="自律登録された Runbook に設定された次回発生時の推奨判断区分",
    )

-- ============================================================================
-- Agentic SRE ハンズオン: BigQuery スキーマ定義 (01_schema.sql)
-- 役割:
--   1. incident_runbooks_raw : JSONL からロードする過去インシデント・Runbook 100 件のステージング表
--   2. incident_runbooks     : AI.EMBED 自動埋め込み生成列（gemini-embedding-2）付きの知識ベース表
--                              新規インシデントを INSERT / MERGE するだけで BigQuery が裏側で自動ベクトル化
--   3. incident_events       : アラート重複排除・クールダウン判定・自動復旧監査用の追記型テーブル
-- 使い方:
--   sed -e "s|__PROJECT_ID__|${GOOGLE_CLOUD_PROJECT}|g" \
--       -e "s|__REGION__|${GOOGLE_CLOUD_REGION}|g" \
--     ./bigquery/01_schema.sql | bq query --project_id="${GOOGLE_CLOUD_PROJECT}" \
--       --location="${GOOGLE_CLOUD_REGION}" --use_legacy_sql=false
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS `sre_agent_ops`
OPTIONS (
  location = '__REGION__',
  description = '常駐自律型 SRE エージェントの自律成長型インシデント知識ベースおよび判断監査ログ'
);

-- 1. 過去インシデント・Runbook 原本ステージング表（初期 100 件の JSONL ロード先）
CREATE OR REPLACE TABLE `sre_agent_ops.incident_runbooks_raw` (
  runbook_id STRING NOT NULL OPTIONS (description = '一意なインシデント・Runbook ID (例: INC-001, AUTO-A1B2C3D4)'),
  category STRING NOT NULL OPTIONS (description = '障害カテゴリ (10分類)'),
  title STRING NOT NULL OPTIONS (description = 'インシデント件名'),
  service_pattern STRING NOT NULL OPTIONS (description = '発生しやすいサービス種別・コンポーネント'),
  symptom_signature STRING NOT NULL OPTIONS (description = '代表的なエラーログ文字列・例外クラス・HTTPステータス'),
  metrics_pattern STRING NOT NULL OPTIONS (description = 'Cloud Monitoring 上の時系列メトリクス変化パターン'),
  recent_change_hint STRING NOT NULL OPTIONS (description = '直近デプロイや設定変更との相関特徴'),
  root_cause STRING NOT NULL OPTIONS (description = '特定された根本原因'),
  recommended_action STRING NOT NULL OPTIONS (description = '推奨判断区分: OBSERVE | AUTO_REMEDIATE | REQUIRE_APPROVAL'),
  remediation_type STRING NOT NULL OPTIONS (description = '復旧操作種別: NONE | ROLLBACK_DEPLOYMENT | RESTART_DEPLOYMENT | MANUAL_INTERVENTION'),
  remediation_steps STRING NOT NULL OPTIONS (description = '具体的な復旧手順または承認後の作業内容'),
  anti_patterns STRING NOT NULL OPTIONS (description = '絶対に実行してはいけない誤対応とその技術的理由'),
  verification_check STRING NOT NULL OPTIONS (description = '処置後に回復したと判定するための確認指標'),
  historical_mttr_minutes INT64 NOT NULL OPTIONS (description = '過去発生時の平均復旧時間（分）')
);

-- 2. 自律埋め込み生成（Autonomous Embedding Generation）有効化の過去インシデント知識ベース表
--    search_document 列に行が追加・更新されると、BigQuery がバックグラウンドで自動的に
--    gemini-embedding-2 エンドポイントを呼び出して embedding 列を生成・維持します。
CREATE TABLE IF NOT EXISTS `sre_agent_ops.incident_runbooks` (
  runbook_id STRING NOT NULL,
  category STRING NOT NULL,
  title STRING NOT NULL,
  service_pattern STRING NOT NULL,
  symptom_signature STRING NOT NULL,
  metrics_pattern STRING NOT NULL,
  recent_change_hint STRING NOT NULL,
  root_cause STRING NOT NULL,
  recommended_action STRING NOT NULL,
  remediation_type STRING NOT NULL,
  remediation_steps STRING NOT NULL,
  anti_patterns STRING NOT NULL,
  verification_check STRING NOT NULL,
  historical_mttr_minutes INT64 NOT NULL,
  search_document STRING NOT NULL OPTIONS (description = 'AI.EMBED が自動ベクトル化する統合検索テキスト'),
  embedding STRUCT<result ARRAY<FLOAT64>, status STRING>
    GENERATED ALWAYS AS (
      AI.EMBED(
        search_document,
        connection_id => '__PROJECT_ID__.__REGION__.sre_genai_conn',
        endpoint => 'gemini-embedding-2'
      )
    ) STORED OPTIONS (asynchronous = TRUE)
);

-- 3. アラート重複排除・サーキットブレーカー・判断監査用の追記型（Append-Only）テーブル
CREATE TABLE IF NOT EXISTS `sre_agent_ops.incident_events` (
  event_id STRING NOT NULL OPTIONS (description = 'イベント行の一意 ID (UUID)'),
  incident_id STRING NOT NULL OPTIONS (description = 'インシデント追跡 ID'),
  fingerprint STRING NOT NULL OPTIONS (description = 'namespace + service + error_code から算出した重複排除キー'),
  namespace STRING NOT NULL OPTIONS (description = '対象 Kubernetes Namespace'),
  service_name STRING NOT NULL OPTIONS (description = '対象サービス / Deployment 名'),
  error_code STRING NOT NULL OPTIONS (description = '検知された代表エラーコード'),
  decision STRING NOT NULL OPTIONS (description = 'OBSERVE | AUTO_REMEDIATE | REQUIRE_APPROVAL | APPROVED_EXECUTED | REJECTED | ADMIN_RESET'),
  remediation_type STRING NOT NULL OPTIONS (description = '実行または提案された復旧操作種別'),
  matched_runbook_id STRING OPTIONS (description = '最も類似度が高かった過去インシデント ID、または自律登録した AUTO-* ID'),
  similarity_score FLOAT64 OPTIONS (description = 'コサイン類似度スコア (0.0〜1.0)'),
  verification_passed BOOL OPTIONS (description = '復旧後のメトリクス回復確認結果'),
  summary STRING OPTIONS (description = 'エージェントが作成した原因分析・処置要約・学習結果'),
  created_at TIMESTAMP NOT NULL OPTIONS (description = '記録時刻 (UTC)')
)
PARTITION BY DATE(created_at)
CLUSTER BY fingerprint, service_name, decision;

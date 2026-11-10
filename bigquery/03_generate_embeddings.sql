-- ============================================================================
-- Agentic SRE ハンズオン: 過去インシデント 100 件の一括ベクトル化と検索確認 (03_generate_embeddings.sql)
-- 前提:
--   bq load --location=asia-northeast1 --source_format=NEWLINE_DELIMITED_JSON --replace \
--     sre_agent_ops.incident_runbooks_raw ./bigquery/seed_runbooks_100.jsonl
--   が完了していること。
-- ============================================================================

-- 1. 100件の症状・メトリクス特徴・直近変更ヒント・根本原因を結合し、一括で埋め込みベクトルを生成
CREATE OR REPLACE TABLE `sre_agent_ops.incident_runbooks` AS
WITH prepared_docs AS (
  SELECT
    *,
    CONCAT(
      'カテゴリ: ', category, '\n',
      '件名: ', title, '\n',
      '対象構成: ', service_pattern, '\n',
      'エラー兆候: ', symptom_signature, '\n',
      'メトリクス推移: ', metrics_pattern, '\n',
      '変更相関: ', recent_change_hint, '\n',
      '根本原因: ', root_cause
    ) AS content
  FROM `sre_agent_ops.incident_runbooks_raw`
)
SELECT
  runbook_id,
  category,
  title,
  service_pattern,
  symptom_signature,
  metrics_pattern,
  recent_change_hint,
  root_cause,
  recommended_action,
  remediation_type,
  remediation_steps,
  anti_patterns,
  verification_check,
  historical_mttr_minutes,
  content AS search_document,
  ml_generate_embedding_result AS embedding
FROM ML.GENERATE_EMBEDDING(
  MODEL `sre_agent_ops.embedding_model`,
  TABLE prepared_docs,
  STRUCT(TRUE AS flatten_json_output)
);

-- 2. 生成件数とベクトル次元数の確認（100 件すべてに非空ベクトルが入っていることを検証）
SELECT
  COUNT(*) AS total_runbooks,
  COUNTIF(ARRAY_LENGTH(embedding) > 0) AS embedded_runbooks,
  ANY_VALUE(ARRAY_LENGTH(embedding)) AS embedding_dimension
FROM `sre_agent_ops.incident_runbooks`;

-- 3. 動作確認用サンプル VECTOR_SEARCH クエリ（設定キー欠落によるデプロイ直後 500 エラーの類似検索）
SELECT
  base.runbook_id,
  base.category,
  base.title,
  base.recommended_action,
  base.remediation_type,
  ROUND(1.0 - distance, 4) AS cosine_similarity
FROM VECTOR_SEARCH(
  TABLE `sre_agent_ops.incident_runbooks`,
  'embedding',
  (
    SELECT ml_generate_embedding_result AS embedding
    FROM ML.GENERATE_EMBEDDING(
      MODEL `sre_agent_ops.embedding_model`,
      (SELECT '新リビジョンデプロイ直後から KeyError 環境変数未設定で HTTP 500 が急増' AS content),
      STRUCT(TRUE AS flatten_json_output)
    )
  ),
  top_k => 3,
  distance_type => 'COSINE'
)
ORDER BY cosine_similarity DESC;

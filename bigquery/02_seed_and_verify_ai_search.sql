-- ============================================================================
-- Agentic SRE ハンズオン: 初期 100 件データの投入と自律埋め込み生成の確認 (02_seed_and_verify_ai_search.sql)
-- 前提:
--   1. Cloud Resource Connection (sre_genai_conn) 作成と AI.EMBED 付きテーブル作成 (01_schema.sql) が完了していること
--   2. bq load で sre_agent_ops.incident_runbooks_raw に seed_runbooks_100.jsonl をロード済みであること
-- ============================================================================

-- 1. ステージング表から incident_runbooks へ初期 100 件を冪等に投入（embedding 列は指定しない）
--    投入直後から BigQuery がバックグラウンドジョブ (gc_*) で gemini-embedding-2 ベクトルを自動生成します。
MERGE `sre_agent_ops.incident_runbooks` AS target
USING (
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
    verified_age_days,
    CONCAT(
      'カテゴリ: ', category, '\n',
      '件名: ', title, '\n',
      '対象構成: ', service_pattern, '\n',
      'エラー兆候: ', symptom_signature, '\n',
      'メトリクス推移: ', metrics_pattern, '\n',
      '変更相関: ', recent_change_hint, '\n',
      '根本原因: ', root_cause
    ) AS search_document
  FROM `sre_agent_ops.incident_runbooks_raw`
) AS src
ON target.runbook_id = src.runbook_id
WHEN NOT MATCHED THEN
  INSERT (
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
    search_document,
    created_at,
    last_verified_at,
    verified_count,
    lifecycle_status
  )
  VALUES (
    src.runbook_id,
    src.category,
    src.title,
    src.service_pattern,
    src.symptom_signature,
    src.metrics_pattern,
    src.recent_change_hint,
    src.root_cause,
    src.recommended_action,
    src.remediation_type,
    src.remediation_steps,
    src.anti_patterns,
    src.verification_check,
    src.historical_mttr_minutes,
    src.search_document,
    TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL (src.verified_age_days + 30) DAY),
    TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL src.verified_age_days DAY),
    CASE WHEN src.verified_age_days <= 90 THEN 3 ELSE 1 END,
    'ACTIVE'
  );

-- 2. バックグラウンド自律埋め込み生成の進捗確認クエリ
--    INSERT 直後は embedded_runbooks が 0 件の場合がありますが、通常 30〜90 秒ほどで 100 件すべて完了します。
SELECT
  COUNT(*) AS total_runbooks,
  COUNTIF(embedding IS NOT NULL AND embedding.status = '') AS embedded_runbooks,
  COUNTIF(embedding IS NOT NULL AND embedding.status != '') AS error_runbooks
FROM `sre_agent_ops.incident_runbooks`;

-- ============================================================================
-- Agentic SRE: この環境でまだ検証していない Runbook の投入 (03_seed_production_starter.sql)
--
-- 対象:
--   1. 本番・PoC 導入用の汎用障害パターン集 (bigquery/seed_runbooks_production_starter.jsonl)
--   2. 既存の手順書を import_markdown_runbooks.py で変換した JSONL
--   ハンズオン用の 100 件 (seed_runbooks_100.jsonl) は 02_seed_and_verify_ai_search.sql で投入します。
--   ハンズオン用の 100 件とは同じデータセットに入れないでください（似た内容の行が検索結果で競合し、訓練の結果が変わります）。
--
-- このSQLが保証すること:
--   - 判断区分は OBSERVE（静観）以外をすべて REQUIRE_APPROVAL（人の承認待ち）にします。
--     JSONL に AUTO_REMEDIATE と書かれていても、自動復旧の対象にはなりません。
--   - 「未検証」として登録します（created_at と last_verified_at は NULL、verified_count は 0）。
--     最終検証日が無い行は鮮度の期限切れとして扱われるため、あとから判断区分だけを AUTO_REMEDIATE に書き換えても、
--     承認と回復確認を 1 回経るまでは自動復旧されません。
--   - 既にある runbook_id は上書きしません（再実行しても重複しません）。
--
-- 使い方:
--   bq load --project_id="${GOOGLE_CLOUD_PROJECT}" --location="${GOOGLE_CLOUD_REGION}" \
--     --source_format=NEWLINE_DELIMITED_JSON --replace \
--     sre_agent_ops.incident_runbooks_raw ./bigquery/seed_runbooks_production_starter.jsonl
--   bq query --project_id="${GOOGLE_CLOUD_PROJECT}" --location="${GOOGLE_CLOUD_REGION}" \
--     --use_legacy_sql=false < ./bigquery/03_seed_production_starter.sql
-- ============================================================================

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
    IF(recommended_action = 'OBSERVE', 'OBSERVE', 'REQUIRE_APPROVAL') AS recommended_action,
    CASE
      WHEN recommended_action = 'OBSERVE' THEN 'NONE'
      WHEN remediation_type IN ('ROLLBACK_DEPLOYMENT', 'RESTART_DEPLOYMENT') THEN remediation_type
      ELSE 'MANUAL_INTERVENTION'
    END AS remediation_type,
    remediation_steps,
    anti_patterns,
    verification_check,
    historical_mttr_minutes,
    -- 02_seed_and_verify_ai_search.sql と同じ組み立て（埋め込みの対象になる検索用テキスト）
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
    CAST(NULL AS TIMESTAMP),
    CAST(NULL AS TIMESTAMP),
    0,
    'ACTIVE'
  );

-- 投入結果の確認。auto_remediate は 0 件になります。
-- embedded は埋め込みの生成が済んだ件数で、投入直後は 0 のことがあります（バックグラウンドで順次生成されます）。
SELECT
  COUNT(*) AS total_runbooks,
  COUNTIF(recommended_action = 'AUTO_REMEDIATE') AS auto_remediate,
  COUNTIF(recommended_action = 'REQUIRE_APPROVAL') AS require_approval,
  COUNTIF(recommended_action = 'OBSERVE') AS observe,
  COUNTIF(verified_count = 0) AS not_yet_verified,
  COUNTIF(embedding IS NOT NULL AND embedding.status = '') AS embedded
FROM `sre_agent_ops.incident_runbooks`;

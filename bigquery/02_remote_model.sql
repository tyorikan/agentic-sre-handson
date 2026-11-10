-- ============================================================================
-- Agentic SRE ハンズオン: BigQuery リモート埋め込みモデル定義 (02_remote_model.sql)
-- 役割:
--   Gemini Enterprise Agent Platform の gemini-embedding-2 モデルを参照する
--   BigQuery ML リモートモデルを作成します。
--   asia-northeast1 データセットから呼び出す場合、BigQuery が自動的に global エンドポイントへルーティングします。
-- ============================================================================

CREATE OR REPLACE MODEL `sre_agent_ops.embedding_model`
REMOTE WITH CONNECTION DEFAULT
OPTIONS (
  ENDPOINT = 'gemini-embedding-2'
);

-- 補足: 組織ポリシーにより DEFAULT 接続が制限されているプロジェクトの場合は、
-- Cloud Shell で以下を実行して明示的コネクションを作成し、CONNECTION に指定してください:
--   bq mk --connection --location=asia-northeast1 --connection_type=CLOUD_RESOURCE sre_vertex_conn
--   CONN_SA=$(bq show --format=json --connection asia-northeast1.sre_vertex_conn | jq -r '.cloudResource.serviceAccountId')
--   gcloud projects add-iam-policy-binding $GOOGLE_CLOUD_PROJECT \
--     --member="serviceAccount:${CONN_SA}" --role="roles/aiplatform.user"

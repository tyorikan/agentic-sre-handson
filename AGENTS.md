# Agentic SRE ハンズオン — AI コーディングエージェント共通規約 (`AGENTS.md`)

このリポジトリで Google Antigravity、Claude Code、Gemini CLI などの AI コーディングエージェントを使用してハンズオンを進める際は、以下の安全ルールと設計規約を必ず遵守してください。

---

## 1. 環境・操作の安全ガードレール（最優先）

1. **検証用プロジェクトの厳守**:
   - すべての `gcloud` / `bq` / `kubectl` コマンドは `.env` に設定された検証用プロジェクト（`$GOOGLE_CLOUD_PROJECT`）のみを対象とし、本番環境のプロジェクトには一切接続しないでください。
2. **状態変更コマンドの事前確認**:
   - API 有効化、IAM 権限付与、GKE クラスタ作成、BigQuery テーブル作成・ロード、Cloud Run instances デプロイ、およびリソース削除コマンドを実行する前に、**実行するコマンド全文を提示し、人間の承認（`y`）を得てから実行**してください。
3. **破壊的コマンドの禁止**:
   - `gcloud projects delete`、承認なしの `gcloud container clusters delete`、`bq rm -r` などの破壊的操作を自律実行しないでください（クリーンアップ章で人間が明示的に指示した場合のみ実行可）。
4. **秘密情報の保護**:
   - Slack トークン（`xoxb-*`, `xapp-*`）やプロジェクト固有の識別子をソースコードや Markdown にハードコードせず、必ず `.env`（Git 管理対象外）または Secret Manager から読み込んでください。

---

## 2. アーキテクチャと実装の原則

1. **モデル名とリージョンの外部化**:
   - 推論モデル（既定: `gemini-3.8-flash`）および埋め込みモデル（既定: `gemini-embedding-2`）は `sre-agent/agent_app/config.py` 経由で環境変数から取得し、廃止済みモデル（`gemini-2.5-*`, `gemini-2.0-*`, `gemini-1.5-*`）は使用しないでください。
   - Cloud Run instances は `us-central1`・`us-east1`・`europe-west1` では利用できないため、既定リージョンは `asia-northeast1`（東京）を使用します。
2. **調査と変更の厳格な分離（Two-Phase Execution Boundary）**:
   - **Phase 1（読み取り調査）**: Google Cloud 公式リモート MCP（Monitoring / Logging / GKE `read-only`）および隔離サンドボックス（`/usr/local/gcp/bin/sandbox do`）のみを使用し、対象クラスタの状態を変更しません。
   - **Phase 2（復旧操作）**: 任意のシェルコマンド（自由な `kubectl` 文字列生成）を LLM に実行させてはなりません。必ず `sre-agent/agent_app/remediation_tools.py` に定義された型付き関数（許可 Namespace 検証・類似度しきい値・Runbook 鮮度有効期限 90 日・1 時間に 1 回までのサーキットブレーカー・事後メトリクス回復確認付き）のみを使用してください。
3. **外部公開エンドポイントの遮断**:
   - Cloud Run instance のデプロイ時は必ず `--sandbox-launcher --ingress internal --no-default-url` を維持し、Slack との双方向承認連携は外向き WebSocket 接続（Slack Socket Mode）で行ってください。
4. **仮想環境での検証**:
   - コード変更後は必ず `.venv` 仮想環境上で `pytest` を実行し、全テストが通過することを確認してください。

# Agentic SRE ハンズオン — AI コーディングエージェント共通規約 (`AGENTS.md`)

このリポジトリで Google Antigravity、Claude Code、Gemini CLI などの AI コーディングエージェントを使用してハンズオンを進める際は、以下の安全ルールと設計規約を遵守してください。

---

## 1. 環境・操作の安全ガードレール（最優先）

1. **検証用プロジェクトの厳守**:
   - すべての `gcloud` / `bq` / `kubectl` / Python 管理スクリプトは `.env` に設定された検証用プロジェクト（`$GOOGLE_CLOUD_PROJECT`）のみを対象とし、本番プロジェクトには一切接続しないでください。
2. **状態変更コマンドの事前確認**:
   - API 有効化、IAM 権限付与、GKE・SLO 作成、BigQuery テーブル作成・ロード、Cloud Run instances デプロイ、および削除コマンドを実行する前に、**実行するコマンド全文を提示し、人間の承認（`y`）を得てから実行**してください。
3. **破壊的コマンドの禁止**:
   - `gcloud projects delete`、未承認の `gcloud container clusters delete`、`bq rm -r` などの破壊的操作を自律実行しないでください。クリーンアップ章で人間が明示的に指示した場合のみ実行可能です。
4. **秘密情報の保護**:
   - Slack トークン（`xoxb-*`, `xapp-*`）や個人・顧客固有の識別子をソースコードや Markdown にハードコードせず、`.env`（Git 管理対象外）または Secret Manager から読み込んでください。
5. **承認と却下は人が行う**:
   - 承認待ちインシデントへの承認と却下は、人が内容を確認して行う操作です。Slack のボタン操作や、承認コマンド `target-app/scripts/approve_incident.py` への `approve`・`reject` の入力を代行しないでください。引数なしで実行する承認待ちの一覧表示は行って構いません。
6. **クイックスタートのスクリプトは、人が一覧を確認してから実行する**:
   - `quickstart/setup.sh` と `quickstart/cleanup.sh` は、多数のリソースをまとめて作成・削除します。実行する前に `--plan` を付けて実行し、表示された対象のプロジェクト ID と、作成または削除するものの一覧を人に提示して、承認（`y`）を得てください。
   - 両スクリプトがプロジェクト ID の入力を求めるのは、対象の取り違えを防ぐためです。人が承認する前に、この入力を自動で流し込まないでください。
   - `quickstart/deploy_agent.sh` も、対象のプロジェクト ID を人に提示して承認を得てから実行してください。

---

## 2. アーキテクチャと実装の原則

1. **モデル名とリージョンの外部化**:
   - 推論モデル（既定: `gemini-4-hydrogen`）および埋め込みモデル（既定: `gemini-embedding-2`）は `sre-agent/agent_app/config.py` 経由で環境変数から取得し、旧世代モデル名をコードへ直書きしないでください。
   - Cloud Run instances は `us-central1`・`us-east1`・`europe-west1` では利用できないため、既定リージョンは `asia-northeast1`（東京）を使用します。
2. **調査と変更の厳格な分離（Two-Phase Execution Boundary）**:
   - **Phase 1（読み取り調査）**: Google 公式リモート MCP（Monitoring / Logging / GKE `read-only` の観測 10 ツール＋Developer Knowledge の公式ドキュメント検索 2 ツール）および隔離サンドボックス（`/usr/local/gcp/bin/sandbox do`）のみを使用し、対象クラスタの状態を変更しません。
   - **Phase 2（復旧操作）**: 任意の `kubectl` シェル文字列を LLM に生成・実行させてはなりません。必ず `sre-agent/agent_app/remediation_tools.py` の型付き関数（許可 Namespace・類似度 `0.85` 以上・Runbook 鮮度 `90` 日以内・1 時間 `1` 回上限・事後エラー率回復確認付き）のみを使用してください。
3. **外部公開エンドポイントの遮断**:
   - Cloud Run instance のデプロイ時は `--sandbox-launcher --ingress internal --no-default-url` を維持し、Slack との双方向承認連携は外向き WebSocket 接続（Slack Socket Mode）で行ってください。
   - Slack の代替ルートも外向き接続だけで構成します。通知メールは Application Integration の API 呼び出しで送り、承認コマンドからの承認は、アラート用とは別の承認専用 Pub/Sub サブスクリプションで受け取ります。承認イベントをアラート用トピックで受け付けないでください。
4. **SLO・Error Budget 効果の誠実な計測**:
   - `sre-agent/agent_app/slo_metrics.py` において、実測 MTTR と温存 Error Budget 分数（Saved Error Budget）は型付き復旧を実行し事後回復が確認されたケースのみ計上し、静観や手動調査引き継ぎ時に架空の短縮時間を計上しないでください。
5. **仮想環境での検証**:
   - コード変更後は必ず `.venv` 仮想環境上で `pytest -v` を実行し、全テスト（112 件）が通過することを確認してください。クイックスタートのスクリプトやフル版のコマンドを変更した場合は、両者の食い違いを `tests/test_quickstart_consistency.py` が検出します。片方だけでなく両方を合わせて変更してください。

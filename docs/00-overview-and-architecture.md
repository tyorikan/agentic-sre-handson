# 第 0 章：全体アーキテクチャと事前準備 (`00-overview-and-architecture.md`)

**所要時間**: 約 20 分  
**到達目標**: 本ワークショップで作る常駐自律型 SRE エージェントの全体像と設計根拠（「なぜ機能するのか」「何が不向きなのか」）を理解し、検証用 Google Cloud プロジェクトの API と IAM を準備する。

---

## 1. このワークショップで体験する自律運用と自己成長ループ

本番システムの運用では、「すべてのアラートで Pod を再起動すればよい」わけではありません。誤った自動操作は障害をかえって拡大させます。本ハンズオンでは、以下の判断フローと解決ナレッジ自律蓄積ループをゼロから構築します。

![アラート検知から 3 分岐・Slack 承認再開までの意思決定フロー図](images/00-01-triage-routing-flow.png)

<details>
<summary>詳細: Mermaid ソースを表示</summary>

```mermaid
flowchart LR
    Alert["アラート・異常ログ検知<br/>Pub/Sub ストリーム受信"] --> Dedup{"クールダウン判定<br/>直近15分以内か?"}
    Dedup -->|"はい"| Skip["重複排除スキップ<br/>LLM 呼び出し 0 回"]
    Dedup -->|"いいえ"| Parallel["テレメトリ補完 & 並列診断<br/>1. ログ時系列エラー率・デプロイ履歴補完<br/>2. 隔離 sandbox 減衰傾き計算<br/>3. BigQuery 過去障害100件照合 + 公式 MCP"]
    Parallel --> Guard{"安全ガードレール検証<br/>許可NS・類似度・回数上限"}

    Guard -->|"一過性エラー・自然減衰中"| S1["シナリオ 1: 静観 OBSERVE<br/>操作せず Slack へ根拠通知"]
    Guard -->|"既知バグ 類似度>=0.85<br/>かつ 1時間以内初回"| S2["シナリオ 2: 自動復旧 AUTO_REMEDIATE<br/>Deployment ロールバック実行<br/>+ エラー率 < 1.0% 回復確認"]
    Guard -->|"未知の DB 障害 / 低確信度<br/>または回数上限到達"| S3["シナリオ 3: 人間承認待ち REQUIRE_APPROVAL<br/>Firestore 保存・ADK 一時停止<br/>Slack Approve / Reject ボタン送信"]
    S3 -->|"Approve 押下"| Resume["ADK 再開・処置実行<br/>初見回復時は BigQuery へ自動ナレッジ昇格"]
    S3 -->|"Reject 押下"| Manual["手動調査へ引き継ぎ"]

    classDef base fill:#ffffff,stroke:#4285F4,stroke-width:1.5px,color:#202124;
    classDef mute fill:#f8f9fa,stroke:#9aa0a6,stroke-width:1.5px,color:#202124;
    classDef ok fill:#e6f4ea,stroke:#137333,stroke-width:2px,color:#202124;
    classDef auto fill:#e8f0fe,stroke:#1967d2,stroke-width:2px,color:#202124;
    classDef hitl fill:#fef7e0,stroke:#b06000,stroke-width:2px,color:#202124;
    classDef warn fill:#fce8e6,stroke:#c5221f,stroke-width:2px,color:#202124;
    class Alert,Dedup,Parallel,Guard base;
    class Skip mute;
    class S1,Resume ok;
    class S2 auto;
    class S3 hitl;
    class Manual warn;
```

</details>

### なぜこの設計が本番で機能するのか／何ができない・不向きなのか

* **本番で機能する理由**:
  1. **Out-of-Band（帯域外）常駐監視**: 監視対象の GKE クラスタとは独立した **Cloud Run instances** 上でエージェントを動かすため、GKE 側がノード枯渇や障害に陥ってもエージェントは巻き込まれず外部から復旧 API を実行できます。
  2. **攻撃対象面ゼロの双方向通信**: Cloud Run instance には `--ingress internal` と `--no-default-url` を設定し、インターネットからの着信を完全遮断します。Pub/Sub StreamingPull と Slack Socket Mode はどちらも**コンテナからの外向き TLS 通信**であるため、公開 URL を一切開けずにリアルタイム監視と承認ボタン連携が成立します。
  3. **調査の柔軟性と変更の厳格性の分離**: メトリクス統計計算などの動的スクリプトは外向き通信・認証情報を遮断した **Cloud Run sandboxes（`sandbox do`）** 内で実行し、GKE の変更操作は Namespace 固定・回数上限付きの型付き Python 関数のみに限定します。
  4. **検証済み解決策のみを蓄積する自律成長型知識ベース**: 初見の障害は安全ガードレールで人間承認に回し、担当者が承認して**事後エラー率 1.0% 未満への回復が実証された場合のみ**、解決内容を構造化 Runbook として BigQuery へ `MERGE` 登録します。テーブルの `GENERATED ALWAYS AS (AI.EMBED(...))` 定義により BigQuery が裏側で自動的にベクトル化するため、再学習バッチ不要で次回から `AI.SEARCH` による自動復旧対象に昇格します。未検証の推測や回復失敗ケースは登録しないため、誤った対処法で知識ベースが汚染されることもありません。
* **できないこと・不向きなこと（設計上の明確な境界線）**:
  1. **全量アクセスログの直接 LLM 監視には不向き**: 毎秒数千行の生ログをすべて LLM に読ませるとコストと遅延が破綻します。一次検知は必ず Cloud Monitoring アラートや Cloud Logging のエラーフィルタシンクで行います。
  2. **DB スキーマ破損やロック競合の無断自動修復は行わない**: ステートフルな DB 障害でアプリを無断再起動するとロールバック負荷と再接続ラッシュで被害が拡大します。本設計ではこれらを自動的に `REQUIRE_APPROVAL` へ振り分け、1 分以内に診断証跡を揃えて人間へ判断を委ねます。
  3. **リージョンと製品ステージの制約**: Cloud Run instances および Cloud Run sandboxes は Preview 機能であり、`us-central1`・`us-east1`・`europe-west1` では利用できません。本ハンズオンでは対応リージョンである **`asia-northeast1`（東京）** を使用します。

---

## 2. 事前準備ステップ（Cloud Shell または手元端末）

![第 0 章の環境変数・API 有効化・最小権限 IAM 準備ステップ](images/00-02-iam-setup-steps.png)

<details>
<summary>詳細: Mermaid ソースを表示</summary>

```mermaid
flowchart LR
    Step1["Step 1: 環境変数 .env 設定<br/>PROJECT_ID / REGION=asia-northeast1"] --> Step2["Step 2: Google Cloud API 有効化<br/>GKE / Cloud Run / BigQuery / Firestore / MCP"]
    Step2 --> Step3["Step 3: エージェント専用 SA 作成<br/>観測読取ロールと最小変更権限を分離"]

    classDef stepBlue fill:#e8f0fe,stroke:#1a73e8,stroke-width:2px,color:#202124;
    classDef stepGreen fill:#e6f4ea,stroke:#137333,stroke-width:2px,color:#202124;
    class Step1,Step2 stepBlue;
    class Step3 stepGreen;
```

</details>

### Step 2.0 実施者（作業ユーザー）に必要な Google Cloud IAM ロールの確認

本ハンズオンではサービスアカウントへの IAM 付与や Kubernetes RBAC の作成を行うため、基本ロールの **編集者（`roles/editor`）だけでは途中で権限エラーになります**。実施者の Google アカウントに以下のいずれかが付与されていることを確認してください。

* **方法 A（個人検証プロジェクト向け・最短）**: 対象プロジェクトの **オーナー（`roles/owner`）**
* **方法 B（組織ポリシー等で `roles/owner` を使わない場合の事前定義ロール一式）**: プロジェクト管理者に依頼し、実施ユーザーへ以下のロールを付与します。

| 事前定義ロール | 使用する章と主な用途 |
|---|---|
| `roles/serviceusage.serviceUsageAdmin` | 第 0 章：各種 Google Cloud API の有効化と利用 |
| `roles/iam.serviceAccountAdmin` | 第 0・6 章：エージェント専用サービスアカウントの作成・削除 |
| `roles/iam.serviceAccountUser` | 第 1・5 章：Cloud Build 実行および Cloud Run instance への SA アタッチ |
| `roles/resourcemanager.projectIamAdmin` | 第 0・2 章：SA への最小権限付与、BigQuery Cloud Resource Connection 用 SA への推論権限付与 |
| `roles/datastore.owner` | 第 0 章：Firestore `(default)` Native データベースの作成 |
| `roles/artifactregistry.admin` | 第 1・6 章：コンテナイメージ用リポジトリの作成・削除 |
| `roles/cloudbuild.builds.editor` / `roles/storage.admin` | 第 1・5 章：`gcloud builds submit` によるソース転送とコンテナビルド |
| `roles/container.admin` | 第 1・5・6 章：GKE Autopilot 作成・削除、`Role` / `RoleBinding` 作成、権限検証 |
| `roles/pubsub.admin` | 第 1・6 章：トピック・サブスクリプション作成、Log Sink Writer への権限付与 |
| `roles/logging.admin` | 第 1・5・6 章：Cloud Logging エラーシンク作成・削除、エージェント構造化ログ閲覧 |
| `roles/bigquery.admin` | 第 2・5・6 章：データセット・Cloud Resource Connection・`AI.EMBED` 自動埋め込みテーブル作成と削除 |
| `roles/run.admin` | 第 5・6 章：Cloud Run instance のデプロイ・停止・削除 |
| `roles/secretmanager.admin` | 第 4・6 章：Slack 連携を行う場合のトークン登録・削除（Slack 省略時は不要） |

```bash
# （方法 B の場合のみ）プロジェクト管理者がハンズオン実施ユーザーへ最小ロール一式を付与するコマンド例
export HANDSON_USER_EMAIL="your-email@example.com"
export GOOGLE_CLOUD_PROJECT="your-handson-project-id"

for ROLE in \
  roles/serviceusage.serviceUsageAdmin \
  roles/iam.serviceAccountAdmin \
  roles/iam.serviceAccountUser \
  roles/resourcemanager.projectIamAdmin \
  roles/datastore.owner \
  roles/artifactregistry.admin \
  roles/cloudbuild.builds.editor \
  roles/storage.admin \
  roles/container.admin \
  roles/pubsub.admin \
  roles/logging.admin \
  roles/bigquery.admin \
  roles/run.admin \
  roles/secretmanager.admin; do
  gcloud projects add-iam-policy-binding "${GOOGLE_CLOUD_PROJECT}" \
    --member="user:${HANDSON_USER_EMAIL}" \
    --role="${ROLE}" \
    --condition=None \
    --quiet
done
```

### Step 2.1 リポジトリの取得と環境変数の設定

検証用の Google Cloud プロジェクト ID を指定して `.env` を作成します（本番プロジェクトは指定しないでください）。

```bash
# HTTPS でクローンする場合（Private 運用中かつ SSH 鍵設定済みの場合は git@github.com:tyorikan/agentic-sre-handson.git）
git clone "https://github.com/tyorikan/agentic-sre-handson.git"
cd agentic-sre-handson

cp .env.example .env
# エディタで GOOGLE_CLOUD_PROJECT をご自身の検証用プロジェクト ID に書き換えます
source .env

gcloud config set project "${GOOGLE_CLOUD_PROJECT}"
```

### Step 2.2 必要な Google Cloud API の有効化と Firestore 作成

GKE、Cloud Run、BigQuery、Firestore、Pub/Sub、Observability、Artifact Registry、Secret Manager、および Gemini Enterprise Agent Platform（`aiplatform.googleapis.com`）を有効化し、ADK セッション・承認待ちチケット用の Firestore Native データベース（`(default)`）を作成します。

```bash
gcloud services enable \
  container.googleapis.com \
  run.googleapis.com \
  bigquery.googleapis.com \
  bigqueryconnection.googleapis.com \
  firestore.googleapis.com \
  pubsub.googleapis.com \
  monitoring.googleapis.com \
  logging.googleapis.com \
  aiplatform.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  --project="${GOOGLE_CLOUD_PROJECT}"

# (default) データベースが未作成の場合のみ作成（既存プロジェクトでは既定 DB が既に存在する場合があります）
if ! gcloud firestore databases describe --database="(default)" --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1; then
  gcloud firestore databases create \
    --location="${GOOGLE_CLOUD_REGION}" \
    --type=firestore-native \
    --project="${GOOGLE_CLOUD_PROJECT}"
fi
```

### Step 2.3 常駐 SRE エージェント専用のサービスアカウント作成

プロジェクト Owner のような過剰な権限は付与せず、ログ・メトリクス閲覧、リモート MCP 呼び出し（`roles/mcp.toolUser`）、BigQuery 読み書き、Firestore 読み書き（`roles/datastore.user`）、Pub/Sub 購読、モデル推論、および GKE API 到達に必要な最小ロールのみを付与します（GKE 内の具体的な操作範囲は次の第 1 章で Kubernetes RBAC により `sre-demo-app` Namespace のみに絞り込みます）。

```bash
export SRE_AGENT_SA_NAME="sre-autonomous-agent-sa"
export SRE_AGENT_SA_EMAIL="${SRE_AGENT_SA_NAME}@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"

gcloud iam service-accounts create "${SRE_AGENT_SA_NAME}" \
  --display-name="Autonomous SRE Agent Service Account" \
  --project="${GOOGLE_CLOUD_PROJECT}"

for ROLE in \
  roles/monitoring.viewer \
  roles/logging.viewer \
  roles/mcp.toolUser \
  roles/serviceusage.serviceUsageConsumer \
  roles/bigquery.dataEditor \
  roles/bigquery.jobUser \
  roles/bigquery.connectionUser \
  roles/datastore.user \
  roles/pubsub.subscriber \
  roles/aiplatform.user \
  roles/container.clusterViewer \
  roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "${GOOGLE_CLOUD_PROJECT}" \
    --member="serviceAccount:${SRE_AGENT_SA_EMAIL}" \
    --role="${ROLE}" \
    --condition=None \
    --quiet
done
```

### 動作確認

以下のコマンドでサービスアカウントと付与ロールが正しく表示されれば準備完了です。

```bash
gcloud projects get-iam-policy "${GOOGLE_CLOUD_PROJECT}" \
  "--flatten=bindings[].members" \
  "--filter=bindings.members:serviceAccount:${SRE_AGENT_SA_EMAIL}" \
  "--format=table(bindings.role)"
```

---

## 🤖 AI コーディングエージェントを使う場合の指示プロンプト

Google Antigravity / Claude Code / Gemini CLI を使って本章を実行する場合は、以下をそのまま渡してください。

```markdown
`AGENTS.md` と `.env` を読み込み、第0章（`docs/00-overview-and-architecture.md`）の事前準備を実行してください。
- 対象プロジェクトが `.env` の `GOOGLE_CLOUD_PROJECT` であることを最初に表示して確認すること。
- API 有効化と `sre-autonomous-agent-sa` サービスアカウント作成・IAM 付与のコマンドを実行前に提示し、私の `y` 承認を得てから実行すること。
```

---

## Links and References

* [Create and manage Cloud Run instances](https://docs.cloud.google.com/run/docs/instances/create-and-manage-instances) — Cloud Run instances の概要と対応リージョン
* [Google Cloud MCP servers overview](https://docs.cloud.google.com/mcp/overview) — `roles/mcp.toolUser` を用いた Google Cloud リモート MCP サーバーの利用方法
* [IAM least privilege best practices](https://docs.cloud.google.com/iam/docs/using-iam-securely) — サービスアカウントの最小権限設計ガイド

次へ進む 👉 [第 1 章：監視対象 GKE アプリとアラートストリームの構築](01-target-gke-and-alerting.md)

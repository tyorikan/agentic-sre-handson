# 第 0 章：全体アーキテクチャと事前準備 (`00-overview-and-architecture.md`)

**所要時間**: 約 20 分  
**到達目標**: 本ワークショップで作る常駐自律型 SRE エージェントの全体像と適用境界を理解し、検証用 Google Cloud プロジェクトの API と最小権限サービスアカウントを準備する。

---

## 1. このワークショップで体験する自律運用と自己成長ループ

本番システムの運用では「すべてのアラートで Pod を再起動すればよい」わけではありません。誤った自動再起動は障害をかえって拡大させます。本ハンズオンでは、以下の判断フロー・解決ナレッジ自律蓄積・Error Budget 温存計測をゼロから構築します。

![アラート検知から 3 分岐・Slack 承認再開までの意思決定フロー図](images/00-01-triage-routing-flow.png)

<details>
<summary>詳細: Mermaid ソースを表示</summary>

```mermaid
flowchart TD
    Alert["1. 異常・SLO急燃焼検知<br/>Pub/Sub ストリーム受信 / CLOSED 除外"] --> Dedup{"2. 15分排他リース判定<br/>Firestore トランザクション共有"}
    Dedup -->|"15分以内"| Skip["重複排除スキップ<br/>LLM 呼び出し 0 回"]
    Dedup -->|"新規障害のみ"| Parallel["3. 補完 & 並列相関診断<br/>直近エラー率 + sandbox 傾き解析<br/>BQ 100件照合 + 公式読取 MCP"]
    Parallel --> Guard{"4. 多層安全ガードレール<br/>許可NS・類似度0.85・推奨一致<br/>デプロイ相関・鮮度90日・1回/h"}

    Guard -->|"自然減衰中"| S1["シナリオ 1: 静観 OBSERVE<br/>操作ゼロ・根拠通知 / 継続中は静観誘導棄却"]
    Guard -->|"既知 >=0.85・鮮度内"| S2["シナリオ 2: 自動復旧 AUTO_REMEDIATE<br/>健全版 Rollback / Restart + エラー率 < 1% 検証"]
    Guard -->|"未知 / 90日超 / 上限"| S3["シナリオ 3・4: 人間承認待ち REQUIRE_APPROVAL<br/>Approve: 処置＋KB登録・鮮度更新 / Reject: 手動引継・凍結"]

    classDef base fill:#ffffff,stroke:#4285F4,stroke-width:1.5px,color:#202124;
    classDef mute fill:#f8f9fa,stroke:#9aa0a6,stroke-width:1.5px,color:#202124;
    classDef ok fill:#e6f4ea,stroke:#137333,stroke-width:2px,color:#202124;
    classDef auto fill:#e8f0fe,stroke:#1967d2,stroke-width:2px,color:#202124;
    classDef hitl fill:#fef7e0,stroke:#b06000,stroke-width:2px,color:#202124;
    class Alert,Dedup,Parallel,Guard base;
    class Skip mute;
    class S1 ok;
    class S2 auto;
    class S3 hitl;
```

</details>

### なぜこの設計が本番で機能するのか／何が不向きなのか

* **本番で機能する 5 つの理由**:
  1. **Out-of-Band（帯域外）常駐監視**: 監視対象の GKE とは独立した **Cloud Run instances** 上で稼働するため、GKE 側が障害に陥っても外部から復旧 API を実行できます。
  2. **着信公開 URL ゼロの双方向通信**: `--ingress internal` と `--no-default-url` で外部からの着信を完全遮断します。Pub/Sub StreamingPull と Slack Socket Mode はどちらもコンテナからの外向き TLS 通信であるため、公開 URL なしで監視と承認ボタン連携が成立します。Slack を使えない場合の代替ルートであるメール通知と承認コマンドも、外向き通信だけで動作します。
  3. **調査の柔軟性と変更の厳格性の分離**: 統計計算スクリプトは外向き通信・認証情報を遮断した **Cloud Run sandboxes（`sandbox do`）** 内で実行し、GKE の変更は Namespace 固定・回数上限付きの型付き関数のみに限定します。
  4. **鮮度管理付きの自律成長型知識ベース**: 初見障害は人間承認を経て**事後エラー率 1.0% 未満への回復が実証された場合のみ** BigQuery `incident_runbooks` へ自動登録し、`AI.EMBED` 生成列で自動ベクトル化します。一方、90 日以上未検証の手順は人間再承認へ自動降格し、却下・未回復手順は自動凍結します。
  5. **Error Budget 温存効果の定量証明**: Cloud Monitoring の公式 SLO モニタリングと連動し、約 1〜2 分の自動復旧によって「従来の人間手動対応［既定 30 分仮定］と比べてどれだけ Error Budget を温存できたか」を推定値として算出し、実測 SLO 残量とともに証明できます。
* **できないこと・不向きなこと（設計上の明確な境界線）**:
  1. **全量生ログの直接 LLM 監視には不向き**: 平常時の大量ログを LLM に直接読ませるとコストと遅延が破綻します。一次抽出は必ず Cloud Logging シンクやアラートで行います。
  2. **DB スキーマ破損やロック競合の無断自動修復は行わない**: ステートフルな DB 障害でアプリを無断再起動すると被害が拡大するため、自動的に `REQUIRE_APPROVAL` へ振り分けて人間へ判断を委ねます。
  3. **リージョンと製品ステージの制約**: Cloud Run instances および sandboxes は Preview（Pre-GA）機能であり、本構成は GA 後の本番適用を見据えた検証用リファレンス設計です。`us-central1`・`us-east1`・`europe-west1` は非対応のため、対応リージョンである **`asia-northeast1`（東京）** を使用します。現時点で SLA 付き GA 基盤が必要な場合は第 6 章の GKE Standard `microVM` 構成を選択してください。

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

本ハンズオンではサービスアカウントへの IAM 付与、Kubernetes RBAC、および SLO 作成を行うため、**編集者（`roles/editor`）だけでは途中で権限不足になります**。以下のいずれかが付与されていることを確認してください。

* **方法 A（個人検証プロジェクト向け・最短）**: 対象プロジェクトの **オーナー（`roles/owner`）**
* **方法 B（最小権限の事前定義ロール一式を使う場合）**: プロジェクト管理者に以下のロール付与を依頼します。

| 事前定義ロール | 使用する章と用途 |
|---|---|
| `roles/serviceusage.serviceUsageAdmin` | 第 0 章：Google Cloud API の有効化と利用 |
| `roles/iam.serviceAccountAdmin` | 第 0・6 章：専用サービスアカウントの作成・削除 |
| `roles/iam.serviceAccountUser` | 第 1・5 章：Cloud Build 実行および Cloud Run instance への SA アタッチ |
| `roles/resourcemanager.projectIamAdmin` | 第 0・2 章：各専用 SA および BigQuery Connection SA への最小権限付与 |
| `roles/datastore.owner` | 第 0 章：Firestore `(default)` Native データベースの作成 |
| `roles/artifactregistry.admin` | 第 1・6 章：コンテナイメージ用リポジトリの作成・削除 |
| `roles/cloudbuild.builds.editor` / `roles/storage.admin` | 第 1・5 章：`gcloud builds submit` によるコンテナビルド |
| `roles/compute.networkAdmin` | 第 1・6 章：専用カスタム VPC・サブネット・Cloud NAT の作成・削除 |
| `roles/container.admin` | 第 1・5・6 章：GKE Autopilot 作成・削除、`Role` / `RoleBinding` 作成 |
| `roles/pubsub.admin` | 第 1・6 章：Pub/Sub トピック・サブスクリプション作成、Sink 権限付与 |
| `roles/logging.admin` | 第 1・5・6 章：Logging シンクおよびログベースメトリクス作成・ログ閲覧 |
| `roles/monitoring.editor` | 第 1・5・6 章：Service Monitoring サービス・可用性 SLO の作成と残量照会 |
| `roles/bigquery.admin` | 第 2・5・6 章：データセット・Cloud Resource Connection・`AI.EMBED` テーブル管理 |
| `roles/run.admin` | 第 5・6 章：Cloud Run instance のデプロイ・停止・削除 |
| `roles/secretmanager.admin` | 第 4・6 章：Slack 連携トークンの登録・削除（Slack 省略時は不要） |
| `roles/integrations.integrationAdmin` | 第 4・6 章：通知メール用の連携の登録・削除（Slack を使う場合は不要） |

```bash
# 方法 B の場合のみ：プロジェクト管理者がハンズオン実施ユーザーへ最小ロール一式を付与するコマンド例
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
  roles/compute.networkAdmin \
  roles/container.admin \
  roles/pubsub.admin \
  roles/logging.admin \
  roles/monitoring.editor \
  roles/bigquery.admin \
  roles/run.admin \
  roles/secretmanager.admin \
  roles/integrations.integrationAdmin; do
  gcloud projects add-iam-policy-binding "${GOOGLE_CLOUD_PROJECT}" \
    --member="user:${HANDSON_USER_EMAIL}" \
    --role="${ROLE}" \
    --condition=None \
    --quiet
done
```

メール通知と承認コマンドの代替ルートは、オーナー権限でのみ検証しています。方法 B の最小ロール構成では、Application Integration のリージョンの有効化に追加の権限が必要になる可能性があります。

### Step 2.1 リポジトリの取得と環境変数の設定

検証用の Google Cloud プロジェクト ID を指定して `.env` を作成します（本番プロジェクトは指定しないでください）。

```bash
git clone "https://github.com/tyorikan/agentic-sre-handson.git"
cd agentic-sre-handson

cp .env.example .env
# エディタで GOOGLE_CLOUD_PROJECT をご自身の検証用プロジェクト ID に書き換えます
source .env

gcloud config set project "${GOOGLE_CLOUD_PROJECT}"
export GOOGLE_CLOUD_QUOTA_PROJECT="${GOOGLE_CLOUD_PROJECT}"

# ローカル端末で実施する場合のみ：Python 管理スクリプト用のユーザ認証情報を設定します（Cloud Shell では不要）
gcloud auth application-default login
gcloud auth application-default set-quota-project "${GOOGLE_CLOUD_PROJECT}"
```

### Step 2.2 Google Cloud API の有効化と Firestore 作成

必要な API を一括有効化し、ADK セッション・承認チケット永続化用の Firestore Native データベース（`(default)`）を作成します。

```bash
gcloud services enable \
  compute.googleapis.com \
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

if ! gcloud firestore databases describe --database="(default)" --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1; then
  gcloud firestore databases create \
    --location="${GOOGLE_CLOUD_REGION}" \
    --type=firestore-native \
    --project="${GOOGLE_CLOUD_PROJECT}"
fi
```

### Step 2.3 用途別に分離した 3 つの最小権限サービスアカウント作成

既定の Compute Engine サービスアカウントが無効化された組織環境でも動作し、かつ過剰権限（`roles/editor` 等）を排除するため、用途別に 3 つの専用サービスアカウントを作成します。

1. **`sre-cloudbuild-sa`**（コンテナビルド専用）：ソース取得・Artifact Registry 書込・ビルドログ出力のみ
2. **`sre-gke-node-sa`**（GKE ノード専用）：ノードからのログ・メトリクス送信とイメージ取得のみ
3. **`sre-autonomous-agent-sa`**（常駐 SRE エージェント専用）：観測・SLO 読取、リモート MCP 呼び出し、BigQuery・Firestore 読み書き、Pub/Sub 購読、モデル推論、GKE クラスタ到達のみ（GKE 内の変更範囲は第 1 章の Kubernetes RBAC で `sre-demo-app` のみに限定）

```bash
export SRE_BUILD_SA_NAME="sre-cloudbuild-sa"
export SRE_BUILD_SA_EMAIL="${SRE_BUILD_SA_NAME}@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"
export GKE_NODE_SA_NAME="sre-gke-node-sa"
export GKE_NODE_SA_EMAIL="${GKE_NODE_SA_NAME}@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"
export SRE_AGENT_SA_NAME="sre-autonomous-agent-sa"
export SRE_AGENT_SA_EMAIL="${SRE_AGENT_SA_NAME}@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"

gcloud iam service-accounts create "${SRE_BUILD_SA_NAME}" \
  --display-name="Cloud Build Least-Privilege SA" \
  --project="${GOOGLE_CLOUD_PROJECT}"

gcloud iam service-accounts create "${GKE_NODE_SA_NAME}" \
  --display-name="GKE Autopilot Node Least-Privilege SA" \
  --project="${GOOGLE_CLOUD_PROJECT}"

gcloud iam service-accounts create "${SRE_AGENT_SA_NAME}" \
  --display-name="Autonomous SRE Agent Service Account" \
  --project="${GOOGLE_CLOUD_PROJECT}"

# SA 作成直後の IAM 伝播待ち（即時実行時の does not exist エラーを防ぐため待機＋リトライ）
sleep 10

grant_roles() {
  local sa_email="$1"
  shift
  for role in "$@"; do
    for attempt in 1 2 3 4 5; do
      if gcloud projects add-iam-policy-binding "${GOOGLE_CLOUD_PROJECT}" \
        --member="serviceAccount:${sa_email}" \
        --role="${role}" \
        --condition=None \
        --quiet >/dev/null; then
        break
      fi
      echo "IAM 伝播待機中 (${sa_email} -> ${role}, attempt=${attempt})..."
      sleep 5
    done
  done
}

grant_roles "${SRE_BUILD_SA_EMAIL}" \
  roles/storage.objectViewer \
  roles/artifactregistry.writer \
  roles/logging.logWriter

grant_roles "${GKE_NODE_SA_EMAIL}" \
  roles/container.defaultNodeServiceAccount \
  roles/artifactregistry.reader

grant_roles "${SRE_AGENT_SA_EMAIL}" \
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
  roles/secretmanager.secretAccessor
```

### 動作確認

以下のコマンドでサービスアカウントに最小ロール一式が付与されていれば準備完了です。

```bash
gcloud projects get-iam-policy "${GOOGLE_CLOUD_PROJECT}" \
  "--flatten=bindings[].members" \
  "--filter=bindings.members:serviceAccount:${SRE_AGENT_SA_EMAIL}" \
  "--format=table(bindings.role)"
```

---

## 🤖 AI コーディングエージェントを使う場合の指示プロンプト

```markdown
`AGENTS.md` と `.env` を読み込み、第0章（`docs/00-overview-and-architecture.md`）の事前準備を実行してください。
- 対象プロジェクトが `.env` の `GOOGLE_CLOUD_PROJECT` であることを最初に表示して確認すること。
- API 有効化・Firestore 作成・3 つの専用 SA 作成と IAM 付与コマンドを実行前に提示し、私の `y` 承認を得てから実行すること。
```

---

## Links and References

* [Create and manage Cloud Run instances](https://docs.cloud.google.com/run/docs/instances/create-and-manage-instances) — Cloud Run instances の概要と対応リージョン
* [Google Cloud MCP servers overview](https://docs.cloud.google.com/mcp/overview) — `roles/mcp.toolUser` を用いた公式リモート MCP サーバーの利用方法
* [IAM least privilege best practices](https://docs.cloud.google.com/iam/docs/using-iam-securely) — サービスアカウントの最小権限設計ガイド
* [Set up Application Default Credentials for a local development environment](https://docs.cloud.google.com/docs/authentication/set-up-adc-local-dev-environment) — ローカル端末と Cloud Shell における ADC の設定方法

次へ進む 👉 [第 1 章：監視対象 GKE・イベントストリーム・可用性 SLO の構築](01-target-gke-and-alerting.md)

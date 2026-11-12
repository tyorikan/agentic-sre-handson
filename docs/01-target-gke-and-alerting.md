# 第 1 章：監視対象 GKE・イベントストリーム・可用性 SLO の構築 (`01-target-gke-and-alerting.md`)

**所要時間**: 約 40 分  
**到達目標**: 監視対象マイクロサービス（`order-service`）と軽量な定常トラフィック Pod を GKE 上に構築し、最小権限 RBAC、「Cloud Logging エラーシンク → Pub/Sub」の即時起動ストリーム、および **Cloud Monitoring Service Monitoring の可用性 SLO（日次 99.0% ＆ 月次 99.9%）** を構成する。

---

## 1. この章で作る場所（現在地マップ）

**監視対象 GKE クラスタ**、**異常時のみの即時イベントストリーム**、および **訓練前後の Error Budget 消費を測る公式 SLO モニタリング** を構築します。

![第 1 章で構築する監視対象 GKE・イベント駆動ストリーム・SLO モニタリングの範囲図](images/01-01-chapter1-scope.png)

<details>
<summary>詳細: Mermaid ソースを表示</summary>

```mermaid
flowchart LR
    subgraph Now1["本章で構築 1: 監視対象 GKE クラスタ"]
        direction TB
        Traffic["定常トラフィック Pod<br/>5秒に1回 POST /orders"]
        App["注文 API: order-service<br/>Namespace: sre-demo-app"]
        RBAC["Role / RoleBinding<br/>sre-demo-app 内の Deployment 更新・参照のみ許可"]
        Traffic -->|"毎分12件"| App
    end

    subgraph Now2["本章で構築 2: 高速検知ストリーム & 公式 SLO"]
        direction TB
        Sink["Cloud Logging シンク<br/>severity >= ERROR のみ抽出"]
        Topic["Cloud Pub/Sub<br/>sre-alert-stream-sub"]
        SLO["Service Monitoring SLO<br/>order_requests_count カウンタ<br/>日次 99.0% & 月次 99.9%"]
        Sink -->|"異常時のみ"| Topic
    end

    subgraph Later["第 2〜5 章で構築・接続"]
        direction TB
        Agent["Cloud Run instance<br/>常駐 SRE エージェント"]
        BQ["BigQuery 100件知識ベース"]
        Agent <-.->|"類似検索"| BQ
    end

    App -->|"異常ログ"| Sink
    App -->|"全注文ログ"| SLO
    Topic -.->|"StreamingPull"| Agent
    SLO -.->|"バジェット残量読取"| Agent
    RBAC <-.->|"型付き復旧 API"| Agent

    classDef active fill:#ffffff,stroke:#1a73e8,stroke-width:2px,color:#202124;
    classDef future fill:#f8f9fa,stroke:#9aa0a6,stroke-width:1.5px,color:#202124;
    class Traffic,App,RBAC,Sink,Topic,SLO active;
    class Agent,BQ future;
```

</details>

### なぜ「高速検知ストリーム」と「SLO モニタリング」の 2 経路に分けるのか？

1. **ミリ秒〜数秒の即時起動＆平時 LLM 課金ゼロ（Log Sink → Pub/Sub）**: Cloud Monitoring のメトリクス集計窓（1 分単位）を待たず、GKE から `severity >= ERROR` の構造化ログが出た瞬間に Pub/Sub へ転送して自律調査を開始します。平常時の `INFO` ログはログルーターで除外されるため、平時の LLM トークン課金はゼロです。
2. **信頼性の定量評価（単一カウンタメトリクス → Service Monitoring SLO）**: 全注文ログ（正常 `201` と異常 `5xx`）を Cloud Logging の単一カウンタメトリクス `order_requests_count` で集計し、第 2〜4 章の作業中から健全なベースラインを蓄積します。これにより第 5 章の障害訓練で「自動復旧によってどれだけ Error Budget 消費を抑えられたか」を実測できます。

![平常時ログ除外と異常発生時のみの Pub/Sub イベント発火シーケンス図](images/01-02-log-routing-sequence.png)

<details>
<summary>詳細: Mermaid ソースを表示</summary>

```mermaid
sequenceDiagram
    autonumber
    participant Client as クライアント / 負荷スクリプト
    participant Pod as GKE Pod: order-service
    participant Router as Cloud Logging Router
    participant PS as Pub/Sub: sre-alert-stream
    participant Agent as SRE Agent: 第5章で接続

    rect rgb(230, 244, 234)
        Note over Client,Router: 平常運用フェーズ — 正常ログは Router で除外され Pub/Sub・LLM 課金ゼロ
        Client->>Pod: POST /orders 正常注文リクエスト
        Pod->>Router: stdout 構造化 JSON 出力 severity=INFO
        Note over Router: シンク条件 severity>=ERROR 不一致のため配信せず終了
    end

    rect rgb(252, 232, 230)
        Note over Client,Agent: 異常発生フェーズ — ERROR / CRITICAL 発生時のみイベント発火
        Client->>Pod: POST /orders 障害発生時リクエスト
        Pod->>Router: stdout 構造化 JSON 出力 severity=ERROR, error_code=...
        Router->>PS: 条件一致ログのみ即時 Publish
        PS-->>Agent: StreamingPull でイベント受信・自律調査を起動
    end
```

</details>

---

## 2. 専用カスタム VPC・Cloud NAT・GKE Autopilot・Artifact Registry の作成

`default` VPC 自動作成無効やノード外部 IP 禁止（`vmExternalIpAccess: DENY`）などのセキュリティ組織ポリシーが有効な環境でもそのまま動作するよう、専用カスタム VPC・Cloud NAT とプライベートノード構成で GKE Autopilot クラスタを作成します。

```bash
source .env

export VPC_NAME="sre-handson-vpc"
export SUBNET_NAME="sre-handson-subnet"
export ROUTER_NAME="sre-handson-router"
export NAT_NAME="sre-handson-nat"
export GKE_NODE_SA_EMAIL="sre-gke-node-sa@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"

# 1. コンテナイメージ用 Artifact Registry リポジトリの作成
gcloud artifacts repositories create sre-handson \
  --repository-format=docker \
  --location="${GOOGLE_CLOUD_REGION}" \
  --description="Agentic SRE Hands-on container repository" \
  --project="${GOOGLE_CLOUD_PROJECT}"

# 2. 専用カスタム VPC とサブネット［Private Google Access 有効］の作成
gcloud compute networks create "${VPC_NAME}" \
  --subnet-mode=custom \
  --project="${GOOGLE_CLOUD_PROJECT}"

gcloud compute networks subnets create "${SUBNET_NAME}" \
  --network="${VPC_NAME}" \
  --region="${GOOGLE_CLOUD_REGION}" \
  --range="10.10.0.0/20" \
  --enable-private-ip-google-access \
  --project="${GOOGLE_CLOUD_PROJECT}"

# 3. プライベートノードからの外向きイメージ取得用 Cloud Router & Cloud NAT 作成
gcloud compute routers create "${ROUTER_NAME}" \
  --network="${VPC_NAME}" \
  --region="${GOOGLE_CLOUD_REGION}" \
  --project="${GOOGLE_CLOUD_PROJECT}"

gcloud compute routers nats create "${NAT_NAME}" \
  --router="${ROUTER_NAME}" \
  --region="${GOOGLE_CLOUD_REGION}" \
  --auto-allocate-nat-external-ips \
  --nat-all-subnet-ip-ranges \
  --project="${GOOGLE_CLOUD_PROJECT}"

# 4. 監視対象 GKE Autopilot クラスタの作成（約 5〜8 分）
gcloud container clusters create-auto "${GKE_CLUSTER_NAME}" \
  --region="${GKE_CLUSTER_LOCATION}" \
  --network="${VPC_NAME}" \
  --subnetwork="${SUBNET_NAME}" \
  --enable-private-nodes \
  --service-account="${GKE_NODE_SA_EMAIL}" \
  --project="${GOOGLE_CLOUD_PROJECT}"

# 5. kubectl 認証情報の取得
gcloud container clusters get-credentials "${GKE_CLUSTER_NAME}" \
  --region="${GKE_CLUSTER_LOCATION}" \
  --project="${GOOGLE_CLOUD_PROJECT}"
```

---

## 3. 最小権限 RBAC・監視対象アプリ・定常トラフィック Pod のデプロイ

プロジェクト全体の `roles/container.developer` や Kubernetes `ClusterRole` は使わず、[target-app/k8s/namespace.yaml](../target-app/k8s/namespace.yaml) で **`sre-demo-app` Namespace 内の `deployments` と `replicasets` のみを更新できる `Role` / `RoleBinding`** を定義します。あわせて、SLO の連続時系列グラフを作るため 5 秒に 1 回（毎分 12 件）注文 API を呼び出す軽量 Pod（`order-traffic-generator`）を配置します。

> [!TIP]
> Cloud Run のメタデータサーバーが払い出すトークンで GKE API を呼ぶ際、GKE 側は SA メールアドレスではなく 21 桁の数値 `uniqueId` を主体として照合する場合があります。`RoleBinding` に両方を登録することで、ローカル実行時と Cloud Run 常駐稼働時の双方で確実に認可されます。

```bash
export SRE_BUILD_SA_EMAIL="sre-cloudbuild-sa@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"
export SRE_AGENT_SA_EMAIL="sre-autonomous-agent-sa@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"
export SRE_AGENT_SA_UNIQUE_ID=$(gcloud iam service-accounts describe "${SRE_AGENT_SA_EMAIL}" --project="${GOOGLE_CLOUD_PROJECT}" --format="value(uniqueId)")
export TARGET_APP_IMAGE_URI="${GOOGLE_CLOUD_REGION}-docker.pkg.dev/${GOOGLE_CLOUD_PROJECT}/sre-handson/order-service"

# 1. サンプル注文 API (v1.0.0) のビルドとプッシュ
gcloud builds submit ./target-app \
  --config=./target-app/cloudbuild.yaml \
  --substitutions="_IMAGE_URI=${TARGET_APP_IMAGE_URI}:v1.0.0" \
  --service-account="projects/${GOOGLE_CLOUD_PROJECT}/serviceAccounts/${SRE_BUILD_SA_EMAIL}" \
  --project="${GOOGLE_CLOUD_PROJECT}"

# 2. Namespace と SRE エージェント用 最小権限 RBAC の適用
sed -e "s|SRE_AGENT_SA_EMAIL|${SRE_AGENT_SA_EMAIL}|g" \
    -e "s|SRE_AGENT_SA_UNIQUE_ID|${SRE_AGENT_SA_UNIQUE_ID}|g" \
  ./target-app/k8s/namespace.yaml | kubectl apply -f -

# 3. 正常版 (v1.0.0) Deployment & Service と定常トラフィック生成 Pod のデプロイ
sed "s|TARGET_APP_IMAGE_URI|${TARGET_APP_IMAGE_URI}|g" \
  ./target-app/k8s/deployment-v1-healthy.yaml | kubectl apply -f -

kubectl rollout status deployment/order-service -n sre-demo-app --timeout=300s

kubectl apply -f ./target-app/k8s/traffic-generator.yaml
kubectl rollout status deployment/order-traffic-generator -n sre-demo-app --timeout=180s
```

### 権限境界の確認（Verification）

`kubectl auth can-i` で、`sre-demo-app` の Deployment 更新のみが許可され、Pod 削除や `kube-system` 変更は拒否されることを確認します。

```bash
kubectl auth can-i patch deployments -n sre-demo-app --as="${SRE_AGENT_SA_EMAIL}"   # 期待出力: yes
kubectl auth can-i delete pods -n sre-demo-app --as="${SRE_AGENT_SA_EMAIL}"         # 期待出力: no
kubectl auth can-i patch deployments -n kube-system --as="${SRE_AGENT_SA_EMAIL}"    # 期待出力: no
```

---

## 4. Pub/Sub トピックと Cloud Logging エラーシンクの構築

`sre-demo-app` から出力される `ERROR` 以上の構造化ログのみをリアルタイム抽出して Pub/Sub へ配信します。

```bash
export ALERT_TOPIC="sre-alert-stream"

gcloud pubsub topics create "${ALERT_TOPIC}" --project="${GOOGLE_CLOUD_PROJECT}"

gcloud pubsub subscriptions create "${ALERT_PUBSUB_SUBSCRIPTION}" \
  --topic="${ALERT_TOPIC}" \
  --ack-deadline=60 \
  --message-retention-duration=1h \
  --project="${GOOGLE_CLOUD_PROJECT}"

gcloud logging sinks create sre-gke-error-sink \
  "pubsub.googleapis.com/projects/${GOOGLE_CLOUD_PROJECT}/topics/${ALERT_TOPIC}" \
  --log-filter='resource.type="k8s_container" AND resource.labels.namespace_name="sre-demo-app" AND severity>=ERROR AND NOT textPayload=~"^INFO:"' \
  --project="${GOOGLE_CLOUD_PROJECT}"

SINK_WRITER_IDENTITY=$(gcloud logging sinks describe sre-gke-error-sink \
  --project="${GOOGLE_CLOUD_PROJECT}" --format="value(writerIdentity)")

gcloud pubsub topics add-iam-policy-binding "${ALERT_TOPIC}" \
  --member="${SINK_WRITER_IDENTITY}" \
  --role="roles/pubsub.publisher" \
  --project="${GOOGLE_CLOUD_PROJECT}"
```

---

## 5. Cloud Monitoring 可用性 SLO・Burn-Rate アラート・エージェント死活監視の構築

[target-app/scripts/setup_slo_monitoring.py](../target-app/scripts/setup_slo_monitoring.py) を実行し、アプリケーション無改修で公式の **Service Monitoring SLO** と **症状ベース検知・死活監視アラート** を一括登録します。

### 単一カウンタメトリクス・2 本の可用性 SLO・症状ベース検知と死活監視の仕組み

* **非同期集計ずれを防ぐ単一メトリクス設計**: 総件数と正常件数を別々のログメトリクスに分けると、1 分境界での非同期フラッシュ差により正常時でも見かけ上の比率割れが起きることがあります。そこで `POST /orders` の構造化ログを単一のカウンタメトリクス **`order_requests_count`**（ラベル `outcome = EXTRACT(severity)`）でアトミックに集計し、1 分間の `outcome="INFO"` 比率が 95% 以上の分を「正常分」と判定する **Windows-based SLI** を構成します。
* **用途に応じた 2 本の可用性 SLO**: 評価期間 $T_{\text{period}}$ に対する許容エラーバジェットは $T_{\text{period}} \times (1 - \text{Goal})$ で定まります。
  1. **訓練観測用・日次 99.0% SLO（`order-availability-daily-99` / カレンダー日次）**: 許容枠 **$14.4\text{ 分/日}$**。約 1〜2 分の障害訓練で残量ゲージが約 7〜14% 動くため、訓練前後の変化をコンソール上で鮮明に確認できます。
  2. **本番換算用・月次 99.9% SLO（`order-availability-monthly-999` / カレンダー月次）**: 30 日換算の許容枠 **$43.2\text{ 分/月}$**。人間手動対応［既定 30 分仮定］だと 1 回の障害で月間バジェットの約 70% を失うのに対し、自動復旧［約 1.3 分］なら約 3% の消費で済む推定温存効果を定量比較できます。
* **ERROR ログ頼みを補う SLO Fast Burn-Rate アラート合流**: Log Sink だけでは ERROR ログを出さない症状悪化を見逃します。本スクリプトは日次 SLO の 10 分間燃焼率（`select_slo_burn_rate` > 10 倍）を監視するアラートポリシーを作成し、既存の `sre-alert-stream` Pub/Sub トピックへ合流させます。復旧完了時の自動クローズ通知（`state="closed"`）はエージェント側で自動的にスキップします。なお、Pod 全滅等でリクエスト自体が完全にゼロになった場合は比率メトリクスの分母が 0 になるため、本番環境では GKE 外形監視（Uptime Check）や Pod 稼働数アラートも併用してください。
* **監視エージェント自身の死活監視（Heartbeat & MetricAbsence）**: エージェント自身が停止した際に同じ Pub/Sub へ通知しても誰も受信できません。そこでエージェントが 60 秒周期で出力する `event="agent_heartbeat"` ログを `sre_agent_heartbeat_count` として集計し、**5 分間途絶した際は人間向けメール通知チャネル（`NOTIFY_EMAIL_TO` 設定時）へ直接発報**する不在検知アラートを登録します。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# ログメトリクス・Service Monitoring・可用性 SLO 2本・Burn-Rate アラート・死活監視アラートを一括作成
python3 ./target-app/scripts/setup_slo_monitoring.py
```

### 動作確認

Pod 一覧（`order-service` 2 台 ＋ `order-traffic-generator` 1 台）が `Running` であり、約 2 分後に `check_slo_budget.py` で達成率 `100.000%`・Error Budget 残量 `100.00%` が表示されることを確認します。

```bash
kubectl get pods -n sre-demo-app
sleep 120
python3 ./target-app/scripts/check_slo_budget.py
```

---

## 🤖 AI コーディングエージェントを使う場合の指示プロンプト

```markdown
`AGENTS.md` と `.env` を読み込み、第1章（`docs/01-target-gke-and-alerting.md`）の手順を実行してください。
- GKE Autopilot クラスタ作成、`order-service:v1.0.0` と `order-traffic-generator` のデプロイ、最小権限 RBAC、Pub/Sub・Logging Sink 作成、および `setup_slo_monitoring.py` の各状態変更コマンドは、実行前に提示して私の `y` 承認を得てから実行すること。
- 最後に `kubectl auth can-i` の 3 つの検証結果と `check_slo_budget.py` の初期ベースライン出力を報告すること。
```

---

## Links and References

* [GKE Autopilot overview](https://docs.cloud.google.com/kubernetes-engine/docs/concepts/autopilot-overview) — GKE Autopilot クラスタの運用モデル
* [Using RBAC authorization in GKE](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/role-based-access-control) — Namespace スコープの最小権限 Role 設計
* [Route logs to supported destinations (Cloud Logging)](https://docs.cloud.google.com/logging/docs/routing/overview) — Cloud Logging シンクによる Pub/Sub へのリアルタイム転送
* [Concepts in service monitoring (Cloud Monitoring)](https://docs.cloud.google.com/stackdriver/docs/solutions/slo-monitoring) — Windows-based / Request-based SLI と Error Budget の評価仕様
* [Using logs-based metrics for SLIs](https://docs.cloud.google.com/stackdriver/docs/solutions/slo-monitoring/sli-metrics/logs-based-metrics) — ログベース カウンタメトリクスを用いた可用性 SLO の構築手順

次へ進む 👉 [第 2 章：BigQuery 自律埋め込み知識ベースと過去インシデント 100 件の構築](02-bigquery-vector-kb.md)

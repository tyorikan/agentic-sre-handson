#!/usr/bin/env bash
# ============================================================================
# クイックスタートの環境構築スクリプト (quickstart/setup.sh)
#
# 役割:
#   フル版の第 0〜2 章と、第 4 章 5.2 の通知メールの準備に相当する環境を、1 回の実行で構築します。
#   エージェントのデプロイは含みません（次の手順の deploy_agent.sh で行います）。
#
# 使い方:
#   ./quickstart/setup.sh          # 作成するものを表示し、プロジェクト ID の入力後に実行する
#   ./quickstart/setup.sh --plan   # 作成するものを表示するだけで、何も変更しない
#
# 再実行:
#   作成済みのものは飛ばします。途中で失敗しても、同じコマンドをもう一度実行すれば続きから進みます。
#
# 中身を読むときの手引き:
#   各段階のコマンドは、フル版のドキュメントに載っているものと同じです。
#   段階名の末尾に、対応するフル版の章を書いています。
# ============================================================================
set -Eeuo pipefail

QS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# env.sh へ、スクリプトから読み込んだことを伝える（仮想環境の有効化と設定の表示を省く）
QS_SOURCED_BY_SCRIPT=1
# shellcheck source=quickstart/env.sh
source "${QS_DIR}/env.sh"
# shellcheck source=quickstart/lib.sh
source "${QS_DIR}/lib.sh"
trap qs_on_error ERR

export CLOUDSDK_CORE_DISABLE_PROMPTS=1
cd "${QS_REPO_ROOT}"

PLAN_ONLY="false"
case "${1:-}" in
  "") ;;
  --plan) PLAN_ONLY="true" ;;
  *) qs_die "使い方: ./quickstart/setup.sh [--plan]" ;;
esac

EMAIL_READY="false"
QS_ACCOUNT=""

# --- 開始前の確認 -------------------------------------------------------------

preflight() {
  qs_require_commands gcloud bq kubectl python3
  command -v gke-gcloud-auth-plugin >/dev/null 2>&1 ||
    qs_die "kubectl から GKE へ接続するための gke-gcloud-auth-plugin が見つかりません。gcloud components install gke-gcloud-auth-plugin で追加してください。"
  python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' ||
    qs_die "Python 3.11 以上が必要です。"
  gcloud beta run instances --help >/dev/null 2>&1 ||
    qs_die "gcloud beta コマンドを使えません。gcloud components install beta で追加してください。"

  QS_ACCOUNT="$(gcloud config get-value account 2>/dev/null || true)"
  [[ -n "${QS_ACCOUNT}" ]] || qs_die "gcloud にログインしていません。gcloud auth login を実行してください。"
  [[ -n "${NOTIFY_EMAIL_TO}" ]] ||
    qs_die ".env の NOTIFY_EMAIL_TO に、通知を受け取るメールアドレスを設定してください。"
  [[ -n "${SRE_TRIAGE_MODEL:-}" && -n "${SRE_EMBEDDING_MODEL:-}" ]] ||
    qs_die ".env に SRE_TRIAGE_MODEL と SRE_EMBEDDING_MODEL がありません。.env.example から .env を作り直してください。"
  gcloud projects describe "${GOOGLE_CLOUD_PROJECT}" --format="value(projectId)" >/dev/null 2>&1 ||
    qs_die "プロジェクト ${GOOGLE_CLOUD_PROJECT} を参照できません。プロジェクト ID と、ログイン中のアカウント ${QS_ACCOUNT} の権限を確認してください。"
}

show_plan() {
  cat >&3 <<EOF

========================================================================
クイックスタートの環境構築
========================================================================
対象プロジェクト      : ${GOOGLE_CLOUD_PROJECT}
実行するアカウント    : ${QS_ACCOUNT}
リージョン            : ${GOOGLE_CLOUD_REGION}
通知メールの宛先      : ${NOTIFY_EMAIL_TO}

作成するもの（作成済みのものは飛ばします）:
  - API の有効化 ${#REQUIRED_APIS[@]} 個
  - Firestore のデータベース (default)
  - サービスアカウント 3 つと、IAM ロール $((${#BUILD_SA_ROLES[@]} + ${#NODE_SA_ROLES[@]} + ${#AGENT_SA_ROLES[@]})) 個の付与
  - VPC ${VPC_NAME} と Cloud NAT
  - GKE Autopilot クラスタ ${GKE_CLUSTER_NAME} と、監視対象アプリ
  - Artifact Registry ${AR_REPOSITORY} と、コンテナイメージ 2 つ
  - BigQuery データセット ${BQ_DATASET}（過去事例 100 件）
  - Pub/Sub トピック 2 つと Log Sink
  - 可用性 SLO 2 本
  - 通知メール用の連携 ${EMAIL_NOTIFY_INTEGRATION}（初回はテストメールを 1 通送ります）
  - 手元の Python 仮想環境 .venv

所要時間の目安 : 約 15 分
費用の目安     : クイックスタートの最後に削除すれば 1 ドル未満（README の料金表を参照）
========================================================================
EOF
}

# --- 各段階 -------------------------------------------------------------------

# 第 0 章 Step 2.2
step_enable_apis() {
  gcloud services enable "${REQUIRED_APIS[@]}" --project="${GOOGLE_CLOUD_PROJECT}"
}

# 第 0 章 Step 2.2
step_firestore() {
  local attempt
  for attempt in 1 2 3 4 5 6; do
    if gcloud firestore databases describe --database="(default)" --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1; then
      echo "Firestore のデータベース (default) は作成済みです。"
      return 0
    fi
    if gcloud firestore databases create \
      --location="${GOOGLE_CLOUD_REGION}" \
      --type=firestore-native \
      --project="${GOOGLE_CLOUD_PROJECT}"; then
      return 0
    fi
    echo "  API の有効化が反映されるのを待って再試行します（${attempt}/6）"
    sleep 10
  done
  return 1
}

# 第 0 章 Step 2.3
step_service_accounts() {
  local created="false" name display
  for name in sre-cloudbuild-sa sre-gke-node-sa sre-autonomous-agent-sa; do
    case "${name}" in
      sre-cloudbuild-sa) display="Cloud Build Least-Privilege SA" ;;
      sre-gke-node-sa) display="GKE Autopilot Node Least-Privilege SA" ;;
      *) display="Autonomous SRE Agent Service Account" ;;
    esac
    if gcloud iam service-accounts describe "${name}@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com" \
      --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1; then
      echo "サービスアカウント ${name} は作成済みです。"
    else
      gcloud iam service-accounts create "${name}" --display-name="${display}" --project="${GOOGLE_CLOUD_PROJECT}"
      created="true"
    fi
  done
  if [[ "${created}" == "true" ]]; then
    # 作成直後は IAM への反映に時間がかかるため、少し待ってからロールを付与する
    sleep 10
  fi
  qs_grant_roles "serviceAccount:${SRE_BUILD_SA_EMAIL}" "${BUILD_SA_ROLES[@]}"
  qs_grant_roles "serviceAccount:${GKE_NODE_SA_EMAIL}" "${NODE_SA_ROLES[@]}"
  qs_grant_roles "serviceAccount:${SRE_AGENT_SA_EMAIL}" "${AGENT_SA_ROLES[@]}"
}

# 第 1 章 2
step_network() {
  gcloud compute networks describe "${VPC_NAME}" --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1 ||
    gcloud compute networks create "${VPC_NAME}" --subnet-mode=custom --project="${GOOGLE_CLOUD_PROJECT}"

  gcloud compute networks subnets describe "${SUBNET_NAME}" --region="${GOOGLE_CLOUD_REGION}" \
    --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1 ||
    gcloud compute networks subnets create "${SUBNET_NAME}" \
      --network="${VPC_NAME}" \
      --region="${GOOGLE_CLOUD_REGION}" \
      --range="10.10.0.0/20" \
      --enable-private-ip-google-access \
      --project="${GOOGLE_CLOUD_PROJECT}"

  gcloud compute routers describe "${ROUTER_NAME}" --region="${GOOGLE_CLOUD_REGION}" \
    --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1 ||
    gcloud compute routers create "${ROUTER_NAME}" \
      --network="${VPC_NAME}" \
      --region="${GOOGLE_CLOUD_REGION}" \
      --project="${GOOGLE_CLOUD_PROJECT}"

  gcloud compute routers nats describe "${NAT_NAME}" --router="${ROUTER_NAME}" --region="${GOOGLE_CLOUD_REGION}" \
    --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1 ||
    gcloud compute routers nats create "${NAT_NAME}" \
      --router="${ROUTER_NAME}" \
      --region="${GOOGLE_CLOUD_REGION}" \
      --auto-allocate-nat-external-ips \
      --nat-all-subnet-ip-ranges \
      --project="${GOOGLE_CLOUD_PROJECT}"
}

gke_cluster_status() {
  gcloud container clusters describe "${GKE_CLUSTER_NAME}" \
    --region="${GKE_CLUSTER_LOCATION}" \
    --project="${GOOGLE_CLOUD_PROJECT}" \
    --format="value(status)" 2>/dev/null || true
}

# 第 1 章 2。作成には 6〜7 分かかるため、完了を待たずに次の段階へ進む
step_gke_start() {
  local status
  status="$(gke_cluster_status)"
  case "${status}" in
    "")
      gcloud container clusters create-auto "${GKE_CLUSTER_NAME}" \
        --region="${GKE_CLUSTER_LOCATION}" \
        --network="${VPC_NAME}" \
        --subnetwork="${SUBNET_NAME}" \
        --enable-private-nodes \
        --service-account="${GKE_NODE_SA_EMAIL}" \
        --project="${GOOGLE_CLOUD_PROJECT}" \
        --async
      ;;
    RUNNING | PROVISIONING | RECONCILING)
      echo "GKE クラスタ ${GKE_CLUSTER_NAME} は作成済み、または作成中です（状態: ${status}）。"
      ;;
    STOPPING)
      echo "GKE クラスタ ${GKE_CLUSTER_NAME} は削除中です。数分待ってから、もう一度実行してください。"
      return 1
      ;;
    *)
      echo "GKE クラスタ ${GKE_CLUSTER_NAME} の状態が ${status} のため、続行できません。"
      echo "./quickstart/cleanup.sh で削除してから、もう一度実行してください。"
      return 1
      ;;
  esac
}

# 第 1 章 5 の仮想環境の準備に相当。クイックスタートでは必要なパッケージだけを入れる
step_python_env() {
  qs_ensure_venv
  if ! "${QS_PY}" -c "import google.auth; google.auth.default()"; then
    echo "手元の認証情報（Application Default Credentials）を取得できません。"
    echo "gcloud auth application-default login を実行してから、もう一度実行してください。"
    return 1
  fi
}

# 第 4 章 5.2
step_email() {
  local code=0
  qs_check_email_integration
  if [[ "${QS_EMAIL_STATE}" == "active" ]]; then
    # 登録済みの場合は、宛先の変更だけを反映する（テストメールは送らない）
    "${QS_PY}" ./target-app/scripts/setup_email_notification.py \
      --min-daily-limit "${QUICKSTART_EMAIL_COUNT}" || code=$?
  else
    "${QS_PY}" ./target-app/scripts/setup_email_notification.py \
      --send-test \
      --min-daily-limit "${QUICKSTART_EMAIL_COUNT}" || code=$?
  fi
  case "${code}" in
    0) EMAIL_READY="true" ;;
    3)
      EMAIL_READY="false"
      qs_say "           注意: このプロジェクトはメールの 1 日の送信上限が ${QUICKSTART_EMAIL_COUNT} 通に足りないため、通知メールの設定を省きました。"
      qs_say "                 構築を待つ間に、フル版 第 4 章 5.1 の手順で Slack を準備してください。"
      ;;
    *) return "${code}" ;;
  esac
}

image_exists() {
  gcloud artifacts docker images describe "$1" --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1
}

build_target_app() {
  gcloud builds submit ./target-app \
    --config=./target-app/cloudbuild.yaml \
    --substitutions="_IMAGE_URI=${TARGET_APP_IMAGE_URI}:v1.0.0" \
    --service-account="projects/${GOOGLE_CLOUD_PROJECT}/serviceAccounts/${SRE_BUILD_SA_EMAIL}" \
    --project="${GOOGLE_CLOUD_PROJECT}"
}

build_agent() {
  gcloud builds submit . \
    --config=./sre-agent/cloudbuild.yaml \
    --substitutions="_IMAGE_URI=${SRE_AGENT_IMAGE_URI}:latest" \
    --service-account="projects/${GOOGLE_CLOUD_PROJECT}/serviceAccounts/${SRE_BUILD_SA_EMAIL}" \
    --project="${GOOGLE_CLOUD_PROJECT}"
}

# 第 1 章 2・3 と、第 5 章 Step 2.1
step_images() {
  gcloud artifacts repositories describe "${AR_REPOSITORY}" --location="${GOOGLE_CLOUD_REGION}" \
    --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1 ||
    gcloud artifacts repositories create "${AR_REPOSITORY}" \
      --repository-format=docker \
      --location="${GOOGLE_CLOUD_REGION}" \
      --description="Agentic SRE Hands-on container repository" \
      --project="${GOOGLE_CLOUD_PROJECT}"

  # サービスアカウントの作成直後は権限の反映待ちで失敗することがあるため、ビルドは再試行する
  if image_exists "${TARGET_APP_IMAGE_URI}:v1.0.0"; then
    echo "監視対象アプリのイメージ v1.0.0 はビルド済みです。"
  else
    qs_retry 3 20 build_target_app
  fi
  if image_exists "${SRE_AGENT_IMAGE_URI}:latest"; then
    echo "エージェントのイメージ latest はビルド済みです。ビルドし直す場合は、フル版 第 5 章 Step 2.1 のコマンドを実行してください。"
  else
    qs_retry 3 20 build_agent
  fi
}

# 第 2 章 3
check_bigquery_embed_ready() {
  bq query \
    --project_id="${GOOGLE_CLOUD_PROJECT}" \
    --location="${GOOGLE_CLOUD_REGION}" \
    --use_legacy_sql=false \
    --quiet \
    "ASSERT (SELECT AI.EMBED('warmup', connection_id => '$1', endpoint => '${SRE_EMBEDDING_MODEL}').status) = '' AS 'IAM propagation pending'"
}

step_bigquery() {
  local connection="${GOOGLE_CLOUD_PROJECT}.${GOOGLE_CLOUD_REGION}.${BQ_CONNECTION_ID}"
  local created="false" connection_sa

  if ! bq show --connection --location="${GOOGLE_CLOUD_REGION}" "${connection}" >/dev/null 2>&1; then
    bq mk --connection \
      --connection_type=CLOUD_RESOURCE \
      --location="${GOOGLE_CLOUD_REGION}" \
      --project_id="${GOOGLE_CLOUD_PROJECT}" \
      "${BQ_CONNECTION_ID}"
    created="true"
  fi

  connection_sa="$(bq show --format=json --connection --location="${GOOGLE_CLOUD_REGION}" "${connection}" |
    python3 -c "import sys, json; print(json.load(sys.stdin)['cloudResource']['serviceAccountId'])")"
  echo "BigQuery の接続用サービスアカウント: ${connection_sa}"

  if [[ "${created}" == "true" ]]; then
    sleep 10
  fi
  qs_grant_roles "serviceAccount:${connection_sa}" roles/aiplatform.user

  # 付与したロールが反映される前にデータを投入すると、初回の自動埋め込みジョブが権限エラーで止まるため、同期呼び出しで反映を確認する
  qs_retry 18 10 check_bigquery_embed_ready "${connection}"

  sed -e "s|__PROJECT_ID__|${GOOGLE_CLOUD_PROJECT}|g" \
    -e "s|__REGION__|${GOOGLE_CLOUD_REGION}|g" \
    ./bigquery/01_schema.sql | bq query \
    --project_id="${GOOGLE_CLOUD_PROJECT}" \
    --location="${GOOGLE_CLOUD_REGION}" \
    --use_legacy_sql=false

  bq load \
    --project_id="${GOOGLE_CLOUD_PROJECT}" \
    --location="${GOOGLE_CLOUD_REGION}" \
    --source_format=NEWLINE_DELIMITED_JSON \
    --replace \
    "${BQ_DATASET}.incident_runbooks_raw" \
    ./bigquery/seed_runbooks_100.jsonl

  bq query \
    --project_id="${GOOGLE_CLOUD_PROJECT}" \
    --location="${GOOGLE_CLOUD_REGION}" \
    --use_legacy_sql=false \
    <./bigquery/02_seed_and_verify_ai_search.sql

  # 以前の実行で権限の反映前に自動埋め込みジョブが止まっていた場合は、1 行を更新してバックグラウンド生成を再開させる
  bq query \
    --project_id="${GOOGLE_CLOUD_PROJECT}" \
    --location="${GOOGLE_CLOUD_REGION}" \
    --use_legacy_sql=false \
    --quiet \
    'IF EXISTS (
       SELECT 1
       FROM `sre_agent_ops`.INFORMATION_SCHEMA.COLUMNS
       WHERE table_name = "incident_runbooks"
         AND column_name = "embedding"
         AND async_generation_status.blocking_error IS NOT NULL
     ) THEN
       UPDATE `sre_agent_ops.incident_runbooks`
       SET search_document = search_document
       WHERE runbook_id = (SELECT MIN(runbook_id) FROM `sre_agent_ops.incident_runbooks`);
     END IF;'

  # 埋め込みは BigQuery が非同期で生成する。全件の完了を待つ（通常 1 分ほど）
  local attempt stats total embedded errors
  for attempt in $(seq 1 30); do
    stats="$(bq query \
      --project_id="${GOOGLE_CLOUD_PROJECT}" \
      --location="${GOOGLE_CLOUD_REGION}" \
      --use_legacy_sql=false \
      --format=csv --quiet \
      'SELECT COUNT(*), COUNTIF(embedding IS NOT NULL AND embedding.status = ""), COUNTIF(embedding IS NOT NULL AND embedding.status != "") FROM `sre_agent_ops.incident_runbooks`' | tail -n 1)"
    IFS=, read -r total embedded errors <<<"${stats}"
    echo "[試行 ${attempt}/30] 埋め込み完了: ${embedded} / ${total} 件、エラー: ${errors} 件"
    if [[ "${total}" -ge 100 && "${embedded}" == "${total}" ]]; then
      return 0
    fi
    sleep 10
  done
  echo "埋め込みの生成が完了しませんでした。1 分ほど待ってから再実行してください。解消しない場合は、フル版 第 2 章を参照してください。"
  return 1
}

# 第 1 章 4 と、第 4 章 5.2
step_pubsub_and_sink() {
  gcloud pubsub topics describe "${ALERT_TOPIC}" --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1 ||
    gcloud pubsub topics create "${ALERT_TOPIC}" --project="${GOOGLE_CLOUD_PROJECT}"

  gcloud pubsub subscriptions describe "${ALERT_PUBSUB_SUBSCRIPTION}" --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1 ||
    gcloud pubsub subscriptions create "${ALERT_PUBSUB_SUBSCRIPTION}" \
      --topic="${ALERT_TOPIC}" \
      --ack-deadline=60 \
      --message-retention-duration=1h \
      --project="${GOOGLE_CLOUD_PROJECT}"

  # 承認コマンドからの承認・却下を受け取る専用のトピック。監視対象のログが流れるアラート用とは分ける
  gcloud pubsub topics describe "${APPROVAL_PUBSUB_TOPIC}" --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1 ||
    gcloud pubsub topics create "${APPROVAL_PUBSUB_TOPIC}" --project="${GOOGLE_CLOUD_PROJECT}"

  gcloud pubsub subscriptions describe "${APPROVAL_PUBSUB_SUBSCRIPTION}" --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1 ||
    gcloud pubsub subscriptions create "${APPROVAL_PUBSUB_SUBSCRIPTION}" \
      --topic="${APPROVAL_PUBSUB_TOPIC}" \
      --ack-deadline=60 \
      --message-retention-duration=10m \
      --project="${GOOGLE_CLOUD_PROJECT}"

  gcloud logging sinks describe sre-gke-error-sink --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1 ||
    gcloud logging sinks create sre-gke-error-sink \
      "pubsub.googleapis.com/projects/${GOOGLE_CLOUD_PROJECT}/topics/${ALERT_TOPIC}" \
      --log-filter='resource.type="k8s_container" AND resource.labels.namespace_name="sre-demo-app" AND severity>=ERROR AND NOT textPayload=~"^INFO:"' \
      --project="${GOOGLE_CLOUD_PROJECT}"

  local writer_identity
  writer_identity="$(gcloud logging sinks describe sre-gke-error-sink \
    --project="${GOOGLE_CLOUD_PROJECT}" --format="value(writerIdentity)")"
  gcloud pubsub topics add-iam-policy-binding "${ALERT_TOPIC}" \
    --member="${writer_identity}" \
    --role="roles/pubsub.publisher" \
    --project="${GOOGLE_CLOUD_PROJECT}" \
    --format=none
}

# 第 1 章 2
step_gke_wait() {
  local status waited=0
  while true; do
    status="$(gke_cluster_status)"
    case "${status}" in
      RUNNING) break ;;
      PROVISIONING | RECONCILING | "") ;;
      *)
        echo "GKE クラスタの状態が ${status} になりました。Google Cloud コンソールの GKE の画面で原因を確認してください。"
        echo "作り直す場合は、./quickstart/cleanup.sh で削除してから、もう一度実行してください。"
        return 1
        ;;
    esac
    if ((waited >= 1800)); then
      echo "30 分待っても GKE クラスタの作成が完了しませんでした（状態: ${status:-不明}）。"
      return 1
    fi
    sleep 15
    waited=$((waited + 15))
  done
  gcloud container clusters get-credentials "${GKE_CLUSTER_NAME}" \
    --region="${GKE_CLUSTER_LOCATION}" \
    --project="${GOOGLE_CLOUD_PROJECT}"
}

apply_namespace_and_rbac() {
  sed -e "s|SRE_AGENT_SA_EMAIL|${SRE_AGENT_SA_EMAIL}|g" \
    -e "s|SRE_AGENT_SA_UNIQUE_ID|$1|g" \
    ./target-app/k8s/namespace.yaml | kubectl apply -f -
}

apply_healthy_release() {
  sed "s|TARGET_APP_IMAGE_URI|${TARGET_APP_IMAGE_URI}|g" \
    ./target-app/k8s/deployment-v1-healthy.yaml | kubectl apply -f -
}

# 第 1 章 3
step_target_app() {
  local unique_id
  unique_id="$(gcloud iam service-accounts describe "${SRE_AGENT_SA_EMAIL}" \
    --project="${GOOGLE_CLOUD_PROJECT}" --format="value(uniqueId)")"

  # 作成直後のクラスタは API サーバーが一時的に応答しないことがあるため、適用は再試行する
  qs_retry 6 20 apply_namespace_and_rbac "${unique_id}"
  qs_retry 6 20 apply_healthy_release
  kubectl rollout status deployment/order-service -n sre-demo-app --timeout=600s

  qs_retry 6 20 kubectl apply -f ./target-app/k8s/traffic-generator.yaml
  kubectl rollout status deployment/order-traffic-generator -n sre-demo-app --timeout=300s
}

# 第 1 章 5
step_slo() {
  "${QS_PY}" ./target-app/scripts/setup_slo_monitoring.py
}

# --- 実行 ---------------------------------------------------------------------

preflight
show_plan
if [[ "${PLAN_ONLY}" == "true" ]]; then
  exit 0
fi
qs_confirm_project "続行するには、対象のプロジェクト ID を入力してください: "
qs_init_log setup

qs_step " 1/13 API を有効化" step_enable_apis
qs_step " 2/13 Firestore のデータベースを作成" step_firestore
qs_step " 3/13 サービスアカウントを作成してロールを付与（約 1 分）" step_service_accounts
qs_step " 4/13 VPC と Cloud NAT を作成" step_network
qs_step " 5/13 GKE Autopilot クラスタの作成を開始（完了は 11/13 で待ちます）" step_gke_start
qs_step " 6/13 手元の Python 環境を準備（初回は約 2 分）" step_python_env
qs_step " 7/13 通知メールを設定" step_email
qs_step " 8/13 コンテナイメージをビルド（約 2〜3 分）" step_images
qs_step " 9/13 BigQuery に知識ベースを構築（約 2 分）" step_bigquery
qs_step "10/13 Pub/Sub と Log Sink を作成" step_pubsub_and_sink
qs_step "11/13 GKE クラスタの作成完了を待つ（作成開始から 6〜7 分）" step_gke_wait
qs_step "12/13 監視対象アプリをデプロイ（約 2〜3 分）" step_target_app
qs_step "13/13 可用性 SLO を登録（約 1 分）" step_slo

qs_print_summary "環境構築が完了しました。段階ごとの所要時間:"
qs_say ""
if [[ "${EMAIL_READY}" == "true" ]]; then
  qs_say "通知メール: 設定済みです（宛先: ${NOTIFY_EMAIL_TO}）。初回はテストメールが届きます。"
else
  qs_say "通知メール: このプロジェクトでは使えません。フル版 第 4 章 5.1 の手順で Slack を準備してから、次へ進んでください。"
fi
qs_say ""
qs_say "次の手順: ./quickstart/deploy_agent.sh"

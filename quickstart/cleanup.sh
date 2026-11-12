#!/usr/bin/env bash
# ============================================================================
# クイックスタートの後片付け (quickstart/cleanup.sh)
#
# 役割:
#   setup.sh と deploy_agent.sh が作成したリソースを削除し、継続的な課金を止めます
#   （フル版 第 6 章 4 の削除手順に相当）。
#   課金額の大きい GKE クラスタと Cloud NAT を先に削除します。
#
# 使い方:
#   ./quickstart/cleanup.sh          # 削除するものの一覧を表示し、プロジェクト ID の入力後に削除する
#   ./quickstart/cleanup.sh --plan   # 削除するものの一覧を表示するだけで、何も変更しない
#
# 削除しないもの:
#   プロジェクト自体、有効化した API、Firestore のデータベース、Application Integration のリージョン、
#   Slack のシークレット、Cloud Build の作業用バケットとログ。
#
# 再実行:
#   既に無いものは飛ばします。途中で失敗しても、同じコマンドをもう一度実行すれば続きから進みます。
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
  *) qs_die "使い方: ./quickstart/cleanup.sh [--plan]" ;;
esac

INSTANCE_NAME="sre-autonomous-agent"
SINK_NAME="sre-gke-error-sink"
SERVICE_ACCOUNT_NAMES=(sre-autonomous-agent-sa sre-gke-node-sa sre-cloudbuild-sa)
FOUND=()

# --- 存在の確認（読み取りのみ） -----------------------------------------------

has_instance() {
  gcloud beta run instances describe "${INSTANCE_NAME}" --region="${GOOGLE_CLOUD_REGION}" \
    --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1
}
gke_cluster_status() {
  gcloud container clusters describe "${GKE_CLUSTER_NAME}" \
    --region="${GKE_CLUSTER_LOCATION}" \
    --project="${GOOGLE_CLOUD_PROJECT}" \
    --format="value(status)" 2>/dev/null || true
}
has_nat() {
  gcloud compute routers nats describe "${NAT_NAME}" --router="${ROUTER_NAME}" --region="${GOOGLE_CLOUD_REGION}" \
    --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1
}
has_router() {
  gcloud compute routers describe "${ROUTER_NAME}" --region="${GOOGLE_CLOUD_REGION}" \
    --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1
}
has_subnet() {
  gcloud compute networks subnets describe "${SUBNET_NAME}" --region="${GOOGLE_CLOUD_REGION}" \
    --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1
}
has_vpc() {
  gcloud compute networks describe "${VPC_NAME}" --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1
}
has_sink() {
  gcloud logging sinks describe "${SINK_NAME}" --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1
}
has_subscription() {
  gcloud pubsub subscriptions describe "$1" --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1
}
has_topic() {
  gcloud pubsub topics describe "$1" --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1
}
has_dataset() {
  bq show --project_id="${GOOGLE_CLOUD_PROJECT}" --dataset "${GOOGLE_CLOUD_PROJECT}:${BQ_DATASET}" >/dev/null 2>&1
}
has_connection() {
  bq show --connection --location="${GOOGLE_CLOUD_REGION}" \
    "${GOOGLE_CLOUD_PROJECT}.${GOOGLE_CLOUD_REGION}.${BQ_CONNECTION_ID}" >/dev/null 2>&1
}
has_repository() {
  gcloud artifacts repositories describe "${AR_REPOSITORY}" --location="${GOOGLE_CLOUD_REGION}" \
    --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1
}
has_service_account() {
  gcloud iam service-accounts describe "$1@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com" \
    --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1
}

# 削除の対象を調べて FOUND へ並べる。各行は表示用の説明
collect_targets() {
  local name
  if has_instance; then FOUND+=("Cloud Run instance: ${INSTANCE_NAME}"); fi
  if [[ -n "$(gke_cluster_status)" ]]; then FOUND+=("GKE クラスタ: ${GKE_CLUSTER_NAME}（監視対象アプリを含む）"); fi
  if has_nat; then FOUND+=("Cloud NAT: ${NAT_NAME}"); fi
  if has_router; then FOUND+=("Cloud Router: ${ROUTER_NAME}"); fi
  if has_subnet; then FOUND+=("サブネット: ${SUBNET_NAME}"); fi
  if has_vpc; then FOUND+=("VPC: ${VPC_NAME}（残っているファイアウォール ルールを含む）"); fi
  # 次の 2 行は Python スクリプトで削除する。存在の確認もそのスクリプトが行う
  FOUND+=("可用性 SLO 2 本・SLO Burn-Rate アラート・エージェント死活監視アラートと各ログベース指標（登録されていれば削除）")
  FOUND+=("通知メール用の連携: ${EMAIL_NOTIFY_INTEGRATION}（登録されていれば削除）")
  if has_sink; then FOUND+=("Log Sink: ${SINK_NAME}"); fi
  for name in "${ALERT_PUBSUB_SUBSCRIPTION}" "${APPROVAL_PUBSUB_SUBSCRIPTION}"; do
    if has_subscription "${name}"; then FOUND+=("Pub/Sub サブスクリプション: ${name}"); fi
  done
  for name in "${ALERT_TOPIC}" "${APPROVAL_PUBSUB_TOPIC}"; do
    if has_topic "${name}"; then FOUND+=("Pub/Sub トピック: ${name}"); fi
  done
  if has_dataset; then FOUND+=("BigQuery データセット: ${BQ_DATASET}（知識ベースと監査記録を含む）"); fi
  if has_connection; then FOUND+=("BigQuery の接続: ${BQ_CONNECTION_ID}"); fi
  if has_repository; then FOUND+=("Artifact Registry: ${AR_REPOSITORY}（コンテナイメージを含む）"); fi
  for name in "${SERVICE_ACCOUNT_NAMES[@]}"; do
    if has_service_account "${name}"; then FOUND+=("サービスアカウント: ${name}（付与したロールを含む）"); fi
  done
}

show_plan() {
  local line
  {
    echo ""
    echo "========================================================================"
    echo "クイックスタートの後片付け"
    echo "========================================================================"
    echo "対象プロジェクト : ${GOOGLE_CLOUD_PROJECT}"
    echo ""
    echo "削除するもの:"
    for line in "${FOUND[@]}"; do
      echo "  - ${line}"
    done
    echo ""
    echo "削除しないもの:"
    echo "  - プロジェクト自体と、有効化した API"
    echo "  - Firestore のデータベース (default) と、その中のセッションと承認の記録"
    echo "  - Application Integration のリージョンと、自動作成された連携 ExecuteConnection"
    echo "  - Slack のシークレット、Cloud Build の作業用バケットとログ"
    echo ""
    echo "所要時間の目安 : 約 10〜15 分（GKE クラスタの削除に 9〜10 分ほどかかります）"
    echo "========================================================================"
  } >&3
}

# --- 各段階 -------------------------------------------------------------------

step_instance() {
  if has_instance; then
    gcloud beta run instances delete "${INSTANCE_NAME}" \
      --region="${GOOGLE_CLOUD_REGION}" \
      --project="${GOOGLE_CLOUD_PROJECT}" \
      --quiet
  else
    echo "Cloud Run instance ${INSTANCE_NAME} は既にありません。"
  fi
}

step_cluster() {
  local status waited=0
  # 作成中や削除中のクラスタは削除を受け付けないため、状態が落ち着くまで待つ
  while true; do
    status="$(gke_cluster_status)"
    case "${status}" in
      "")
        echo "GKE クラスタ ${GKE_CLUSTER_NAME} は既にありません。"
        return 0
        ;;
      PROVISIONING | RECONCILING | STOPPING)
        if ((waited >= 1800)); then
          echo "30 分待っても GKE クラスタの状態が ${status} のままです。時間をおいて、もう一度実行してください。"
          return 1
        fi
        echo "  GKE クラスタの状態が ${status} のため、完了を待ちます"
        sleep 20
        waited=$((waited + 20))
        ;;
      *) break ;;
    esac
  done
  gcloud container clusters delete "${GKE_CLUSTER_NAME}" \
    --region="${GKE_CLUSTER_LOCATION}" \
    --project="${GOOGLE_CLOUD_PROJECT}" \
    --quiet
}

delete_subnet() {
  gcloud compute networks subnets delete "${SUBNET_NAME}" \
    --region="${GOOGLE_CLOUD_REGION}" \
    --project="${GOOGLE_CLOUD_PROJECT}" \
    --quiet
}

delete_vpc() {
  gcloud compute networks delete "${VPC_NAME}" \
    --project="${GOOGLE_CLOUD_PROJECT}" \
    --quiet
}

step_network() {
  if has_nat; then
    gcloud compute routers nats delete "${NAT_NAME}" \
      --router="${ROUTER_NAME}" \
      --region="${GOOGLE_CLOUD_REGION}" \
      --project="${GOOGLE_CLOUD_PROJECT}" \
      --quiet
  fi
  if has_router; then
    gcloud compute routers delete "${ROUTER_NAME}" \
      --region="${GOOGLE_CLOUD_REGION}" \
      --project="${GOOGLE_CLOUD_PROJECT}" \
      --quiet
  fi
  if has_vpc; then
    # GKE がこの VPC に作ったファイアウォール ルールが残っていると VPC を削除できないため、先に削除する
    local rule rules
    rules="$(gcloud compute firewall-rules list \
      --project="${GOOGLE_CLOUD_PROJECT}" \
      --filter="network~/${VPC_NAME}\$" \
      --format="value(name)")"
    for rule in ${rules}; do
      gcloud compute firewall-rules delete "${rule}" --project="${GOOGLE_CLOUD_PROJECT}" --quiet ||
        echo "  ファイアウォール ルール ${rule} は削除できませんでした（既に削除されている場合は問題ありません）。"
    done
  fi
  # クラスタの削除直後は、サブネットと VPC の解放が終わっていないことがあるため再試行する
  if has_subnet; then
    qs_retry 6 20 delete_subnet
  fi
  if has_vpc; then
    qs_retry 6 20 delete_vpc
  fi
}

# SLO と通知メール用の連携は Python スクリプトで削除する（どちらも、既に無ければ何もしない）
step_slo_and_email() {
  qs_ensure_venv
  "${QS_PY}" ./target-app/scripts/setup_slo_monitoring.py --delete
  "${QS_PY}" ./target-app/scripts/setup_email_notification.py --delete
}

step_sink_and_pubsub() {
  local name
  if has_sink; then
    gcloud logging sinks delete "${SINK_NAME}" --project="${GOOGLE_CLOUD_PROJECT}" --quiet
  fi
  for name in "${ALERT_PUBSUB_SUBSCRIPTION}" "${APPROVAL_PUBSUB_SUBSCRIPTION}"; do
    if has_subscription "${name}"; then
      gcloud pubsub subscriptions delete "${name}" --project="${GOOGLE_CLOUD_PROJECT}" --quiet
    fi
  done
  for name in "${ALERT_TOPIC}" "${APPROVAL_PUBSUB_TOPIC}"; do
    if has_topic "${name}"; then
      gcloud pubsub topics delete "${name}" --project="${GOOGLE_CLOUD_PROJECT}" --quiet
    fi
  done
}

step_bigquery() {
  if has_connection; then
    # 接続用のサービスアカウントへ付与したロールを、接続を消す前に取り消す
    local connection_sa
    connection_sa="$(bq show --format=json --connection --location="${GOOGLE_CLOUD_REGION}" \
      "${GOOGLE_CLOUD_PROJECT}.${GOOGLE_CLOUD_REGION}.${BQ_CONNECTION_ID}" |
      python3 -c "import sys, json; print(json.load(sys.stdin)['cloudResource']['serviceAccountId'])")"
    qs_revoke_roles "serviceAccount:${connection_sa}" roles/aiplatform.user
  fi
  if has_dataset; then
    bq rm -r -f -d "${GOOGLE_CLOUD_PROJECT}:${BQ_DATASET}"
  fi
  if has_connection; then
    bq rm --connection --location="${GOOGLE_CLOUD_REGION}" --project_id="${GOOGLE_CLOUD_PROJECT}" -f "${BQ_CONNECTION_ID}"
  fi
}

step_repository() {
  if has_repository; then
    gcloud artifacts repositories delete "${AR_REPOSITORY}" \
      --location="${GOOGLE_CLOUD_REGION}" \
      --project="${GOOGLE_CLOUD_PROJECT}" \
      --quiet
  fi
}

step_service_accounts() {
  local name email
  for name in "${SERVICE_ACCOUNT_NAMES[@]}"; do
    if ! has_service_account "${name}"; then
      continue
    fi
    email="${name}@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"
    # ロールを付けたままサービスアカウントを消すと、IAM ポリシーに削除済みの主体が残るため、先に取り消す
    case "${name}" in
      sre-cloudbuild-sa) qs_revoke_roles "serviceAccount:${email}" "${BUILD_SA_ROLES[@]}" ;;
      sre-gke-node-sa) qs_revoke_roles "serviceAccount:${email}" "${NODE_SA_ROLES[@]}" ;;
      *) qs_revoke_roles "serviceAccount:${email}" "${AGENT_SA_ROLES[@]}" ;;
    esac
    gcloud iam service-accounts delete "${email}" --project="${GOOGLE_CLOUD_PROJECT}" --quiet
  done
}

# --- 実行 ---------------------------------------------------------------------

qs_require_commands gcloud bq python3
echo "削除の対象を調べています（読み取りのみ）..." >&3
collect_targets
show_plan
if [[ "${PLAN_ONLY}" == "true" ]]; then
  exit 0
fi
qs_confirm_project "上記を削除します。続行するには、対象のプロジェクト ID を入力してください: "
qs_init_log cleanup

qs_step "1/8 Cloud Run instance を削除" step_instance
qs_step "2/8 GKE クラスタを削除（数分かかります）" step_cluster
qs_step "3/8 Cloud NAT・Cloud Router・サブネット・VPC を削除" step_network
qs_step "4/8 SLO と通知メール用の連携を削除" step_slo_and_email
qs_step "5/8 Log Sink と Pub/Sub を削除" step_sink_and_pubsub
qs_step "6/8 BigQuery のデータセットと接続を削除" step_bigquery
qs_step "7/8 Artifact Registry を削除" step_repository
qs_step "8/8 サービスアカウントを削除（約 1 分）" step_service_accounts

qs_print_summary "後片付けが完了しました。段階ごとの所要時間:"
qs_say ""
qs_say "プロジェクト自体が不要な場合は、Google Cloud コンソールの「IAM と管理」からシャットダウンできます。"

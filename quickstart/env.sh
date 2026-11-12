#!/usr/bin/env bash
# ============================================================================
# クイックスタートの共通設定 (quickstart/env.sh)
#
# 役割:
#   .env を読み込み、クイックスタートのスクリプトと訓練のコマンドが使う変数をそろえます。
#
# 使い方:
#   新しいターミナルを開いたら、リポジトリのルートで最初に 1 回実行します。
#     source ./quickstart/env.sh
#
# 補足:
#   .env に書かれた値のうち、Slack のトークンなどの秘密情報は export しません。
# ============================================================================

_QS_ENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
QS_REPO_ROOT="$(cd "${_QS_ENV_DIR}/.." && pwd)"

if [[ ! -f "${QS_REPO_ROOT}/.env" ]]; then
  echo "エラー: ${QS_REPO_ROOT}/.env がありません。cp .env.example .env を実行し、2 項目を編集してください。" >&2
  return 1 2>/dev/null || exit 1
fi

# shellcheck disable=SC1091
source "${QS_REPO_ROOT}/.env"

if [[ -z "${GOOGLE_CLOUD_PROJECT:-}" || "${GOOGLE_CLOUD_PROJECT}" == "your-sandbox-project-id" ]]; then
  echo "エラー: .env の GOOGLE_CLOUD_PROJECT を、検証用プロジェクトの ID に書き換えてください。" >&2
  return 1 2>/dev/null || exit 1
fi

# --- .env.example と同じ既定値（.env で省略されていても動くようにする） ---
: "${GOOGLE_CLOUD_REGION:=asia-northeast1}"
: "${GKE_CLUSTER_NAME:=sre-demo-cluster}"
: "${GKE_CLUSTER_LOCATION:=${GOOGLE_CLOUD_REGION}}"
: "${ALLOWED_K8S_NAMESPACES:=sre-demo-app}"
: "${ALERT_PUBSUB_SUBSCRIPTION:=sre-alert-stream-sub}"
: "${BQ_DATASET:=sre_agent_ops}"
: "${BQ_CONNECTION_ID:=sre_genai_conn}"
: "${ENABLE_DEVELOPER_KNOWLEDGE_MCP:=true}"
: "${SLACK_ALERT_CHANNEL_ID:=}"
: "${SLACK_ALLOWED_APPROVER_IDS:=}"
: "${NOTIFY_EMAIL_TO:=}"

# --- クイックスタートの標準ルート: メール通知と承認コマンド（.env で空のままでも有効にする） ---
: "${EMAIL_NOTIFY_INTEGRATION:=sre-notify-email}"
: "${APPROVAL_PUBSUB_TOPIC:=sre-approval-decisions}"
: "${APPROVAL_PUBSUB_SUBSCRIPTION:=sre-approval-decisions-sub}"

# --- フル版の各章で export している値と同じ名前の、組み立て済みの値 ---
ALERT_TOPIC="sre-alert-stream"
AR_REPOSITORY="sre-handson"
VPC_NAME="sre-handson-vpc"
SUBNET_NAME="sre-handson-subnet"
ROUTER_NAME="sre-handson-router"
NAT_NAME="sre-handson-nat"
SRE_BUILD_SA_EMAIL="sre-cloudbuild-sa@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"
GKE_NODE_SA_EMAIL="sre-gke-node-sa@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"
SRE_AGENT_SA_EMAIL="sre-autonomous-agent-sa@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com"
TARGET_APP_IMAGE_URI="${GOOGLE_CLOUD_REGION}-docker.pkg.dev/${GOOGLE_CLOUD_PROJECT}/${AR_REPOSITORY}/order-service"
SRE_AGENT_IMAGE_URI="${GOOGLE_CLOUD_REGION}-docker.pkg.dev/${GOOGLE_CLOUD_PROJECT}/${AR_REPOSITORY}/sre-autonomous-agent"

# 手元の認証情報で API を呼ぶ Python スクリプトが、このプロジェクトの割り当てを使うようにする
GOOGLE_CLOUD_QUOTA_PROJECT="${GOOGLE_CLOUD_PROJECT}"

export GOOGLE_CLOUD_PROJECT GOOGLE_CLOUD_REGION GOOGLE_CLOUD_QUOTA_PROJECT
export GKE_CLUSTER_NAME GKE_CLUSTER_LOCATION ALERT_PUBSUB_SUBSCRIPTION BQ_DATASET BQ_CONNECTION_ID
export EMAIL_NOTIFY_INTEGRATION APPROVAL_PUBSUB_TOPIC APPROVAL_PUBSUB_SUBSCRIPTION NOTIFY_EMAIL_TO
export TARGET_APP_IMAGE_URI SRE_AGENT_IMAGE_URI

# ターミナルから直接読み込んだ場合は、仮想環境を有効にして設定を表示する
# （setup.sh などのスクリプトから読み込まれた場合は、何も表示しない）
if [[ -z "${QS_SOURCED_BY_SCRIPT:-}" ]]; then
  if [[ -f "${QS_REPO_ROOT}/.venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "${QS_REPO_ROOT}/.venv/bin/activate"
  fi
  echo "クイックスタートの設定を読み込みました: プロジェクト=${GOOGLE_CLOUD_PROJECT} / リージョン=${GOOGLE_CLOUD_REGION} / 通知先=${NOTIFY_EMAIL_TO:-未設定}"
fi

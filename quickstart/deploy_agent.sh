#!/usr/bin/env bash
# ============================================================================
# クイックスタートのエージェントのデプロイ (quickstart/deploy_agent.sh)
#
# 役割:
#   常駐 SRE エージェントを Cloud Run instance としてデプロイします（フル版 第 5 章 Step 2.2 に相当）。
#   着信 URL を持たない設定（--ingress=internal と --no-default-url）と、
#   隔離サンドボックスの有効化（--sandbox-launcher）は、フル版と同じです。
#
# 通知と承認の経路:
#   - メール通知     : setup.sh が通知メール用の連携を登録済みなら有効にします。
#   - 承認コマンド   : 常に有効にします。
#   - Slack          : Slack のシークレット 2 つがあり、.env に実際のチャンネル ID があれば有効にします。
#   メール通知と Slack のどちらも使えない場合は、通知が届かないためデプロイを中止します。
#
# 使い方:
#   ./quickstart/deploy_agent.sh
#
# 注意:
#   ALLOW_DRILL_COOLDOWN_RESET=true は、訓練を続けて行うための設定です。本番運用では false にしてください。
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

INSTANCE_NAME="sre-autonomous-agent"
EMAIL_ENABLED="false"
SLACK_ENABLED="false"
SECRET_FLAGS=()
STARTED_AT=""

[[ -x "${QS_PY}" ]] || qs_die "仮想環境 .venv がありません。先に ./quickstart/setup.sh を実行してください。"

# 使える通知の経路を調べる
step_detect_routes() {
  qs_check_email_integration
  if [[ "${QS_EMAIL_STATE}" == "active" ]]; then
    EMAIL_ENABLED="true"
  fi

  if gcloud secrets describe sre-slack-bot-token --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1 &&
    gcloud secrets describe sre-slack-app-token --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1 &&
    [[ -n "${SLACK_ALERT_CHANNEL_ID}" && "${SLACK_ALERT_CHANNEL_ID}" != "C0000000000" ]]; then
    SLACK_ENABLED="true"
  fi

  if [[ "${EMAIL_ENABLED}" != "true" && "${SLACK_ENABLED}" != "true" ]]; then
    echo "通知の経路がありません。次のどちらかを行ってから、もう一度実行してください。"
    echo "  - メール通知: ./quickstart/setup.sh をもう一度実行する"
    echo "  - Slack     : フル版 第 4 章 5.1 の手順で Slack を準備する"
    return 1
  fi
}

step_deploy() {
  local slack_channel="" slack_approvers=""
  if [[ "${SLACK_ENABLED}" == "true" ]]; then
    slack_channel="${SLACK_ALERT_CHANNEL_ID}"
    slack_approvers="${SLACK_ALLOWED_APPROVER_IDS}"
    SECRET_FLAGS=("--set-secrets=SLACK_BOT_TOKEN=sre-slack-bot-token:latest,SLACK_APP_TOKEN=sre-slack-app-token:latest")
  elif gcloud beta run instances describe "${INSTANCE_NAME}" --region="${GOOGLE_CLOUD_REGION}" \
    --project="${GOOGLE_CLOUD_PROJECT}" >/dev/null 2>&1; then
    # 以前のデプロイで Slack を使っていた場合に、シークレットの参照を外す
    SECRET_FLAGS=("--clear-secrets")
  fi

  local email_integration=""
  if [[ "${EMAIL_ENABLED}" == "true" ]]; then
    email_integration="${EMAIL_NOTIFY_INTEGRATION}"
  fi

  # エージェントへ渡す環境変数。フル版 第 5 章 Step 2.2 のコマンドと同じ内容にする。
  # 通知と承認の経路を決める末尾の 4 つだけは、上で調べた結果から値を決める
  local env_vars=(
    "GOOGLE_CLOUD_PROJECT=${GOOGLE_CLOUD_PROJECT}"
    "GOOGLE_CLOUD_REGION=${GOOGLE_CLOUD_REGION}"
    "GOOGLE_CLOUD_LOCATION=global"
    "GOOGLE_GENAI_USE_ENTERPRISE=True"
    "SRE_TRIAGE_MODEL=${SRE_TRIAGE_MODEL}"
    "SRE_EMBEDDING_MODEL=${SRE_EMBEDDING_MODEL}"
    "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false"
    "ALERT_PUBSUB_SUBSCRIPTION=${ALERT_PUBSUB_SUBSCRIPTION}"
    "BQ_DATASET=${BQ_DATASET}"
    "BQ_CONNECTION_ID=${BQ_CONNECTION_ID:-sre_genai_conn}"
    "ENABLE_AUTONOMOUS_KB_LEARNING=${ENABLE_AUTONOMOUS_KB_LEARNING:-true}"
    "RUNBOOK_FRESHNESS_TTL_DAYS=${RUNBOOK_FRESHNESS_TTL_DAYS:-90}"
    "ALLOW_DRILL_COOLDOWN_RESET=true"
    "GKE_CLUSTER_NAME=${GKE_CLUSTER_NAME}"
    "GKE_CLUSTER_LOCATION=${GKE_CLUSTER_LOCATION}"
    "ALLOWED_K8S_NAMESPACES=${ALLOWED_K8S_NAMESPACES}"
    "SANDBOX_MODE=cloud_run"
    "INITIAL_STABILIZATION_SECONDS=30"
    "SLO_SERVICE_ID=${SLO_SERVICE_ID:-order-service-slo}"
    "SLO_DAILY_ID=${SLO_DAILY_ID:-order-availability-daily-99}"
    "SLO_MONTHLY_ID=${SLO_MONTHLY_ID:-order-availability-monthly-999}"
    "MANUAL_BASELINE_MTTR_MINUTES=${MANUAL_BASELINE_MTTR_MINUTES:-30.0}"
    "SESSION_BACKEND=firestore"
    "FIRESTORE_DATABASE=(default)"
    "SLACK_ALERT_CHANNEL_ID=${slack_channel}"
    "SLACK_ALLOWED_APPROVER_IDS=${slack_approvers}"
    "EMAIL_NOTIFY_INTEGRATION=${email_integration}"
    "APPROVAL_PUBSUB_SUBSCRIPTION=${APPROVAL_PUBSUB_SUBSCRIPTION}"
  )
  # 値にカンマを含められるよう、区切り文字を @ に変えて 1 つの引数へまとめる
  local joined
  joined="$(
    IFS=@
    echo "${env_vars[*]}"
  )"

  STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  gcloud beta run instances deploy "${INSTANCE_NAME}" \
    --image="${SRE_AGENT_IMAGE_URI}:latest" \
    --region="${GOOGLE_CLOUD_REGION}" \
    --service-account="${SRE_AGENT_SA_EMAIL}" \
    --restart-policy=always \
    --port=8080 \
    --cpu=2 \
    --memory=2Gi \
    --sandbox-launcher \
    --ingress=internal \
    --no-default-url \
    --set-env-vars="^@^${joined}" \
    ${SECRET_FLAGS[@]+"${SECRET_FLAGS[@]}"} \
    --project="${GOOGLE_CLOUD_PROJECT}"
}

# エージェントが起動時に出すログから、有効になった経路を読み取る
step_wait_startup() {
  local attempt routes
  for attempt in $(seq 1 18); do
    routes="$(gcloud logging read \
      "resource.labels.instance_name=\"${INSTANCE_NAME}\" AND jsonPayload.message:\"通知と承認の経路\" AND timestamp>=\"${STARTED_AT}\"" \
      --limit=1 \
      --project="${GOOGLE_CLOUD_PROJECT}" \
      --format='value(jsonPayload.slack_enabled,jsonPayload.email_enabled,jsonPayload.approval_command_enabled)' 2>/dev/null || true)"
    if [[ -n "${routes}" ]]; then
      echo "起動ログを確認しました（Slack・メール通知・承認コマンドの順）: ${routes}"
      return 0
    fi
    echo "  起動ログを待っています（${attempt}/18）"
    sleep 10
  done
  echo "3 分待っても起動ログを確認できませんでした。次のコマンドでログを確認してください。"
  echo "  gcloud beta run instances logs read ${INSTANCE_NAME} --region ${GOOGLE_CLOUD_REGION} --project ${GOOGLE_CLOUD_PROJECT}"
  return 1
}

route_label() {
  if [[ "$1" == "true" ]]; then
    echo "有効"
  else
    echo "無効"
  fi
}

echo "対象プロジェクト: ${GOOGLE_CLOUD_PROJECT}" >&3
qs_init_log deploy

qs_step "1/3 使える通知の経路を確認" step_detect_routes
qs_step "2/3 エージェントを Cloud Run instance へデプロイ（約 1 分）" step_deploy
qs_step "3/3 エージェントの起動を確認" step_wait_startup

qs_print_summary "デプロイが完了しました。段階ごとの所要時間:"
qs_say ""
qs_say "通知と承認の経路:"
qs_say "  メール通知   : $(route_label "${EMAIL_ENABLED}")"
qs_say "  承認コマンド : 有効"
qs_say "  Slack        : $(route_label "${SLACK_ENABLED}")"
qs_say ""
qs_say "次の手順: source ./quickstart/env.sh を実行してから、訓練 1 (./target-app/scripts/inject_transient_net_err.sh) へ進みます。"

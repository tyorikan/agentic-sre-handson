#!/usr/bin/env bash
# ============================================================================
# 訓練後に各 Pod の擬似障害モードを NONE へ戻すリセットスクリプト
# オプション:
#   --reset-cooldown : Pub/Sub 経由で ADMIN_COOLDOWN_RESET を送信し、
#                      15分クールダウンと1時間1回上限を即時解除する（連続訓練用）
# ============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-sre-demo-app}"
SERVICE="${SERVICE:-order-service}"
TOPIC="${ALERT_PUBSUB_TOPIC:-sre-alert-stream}"
RESET_COOLDOWN="false"

if [[ "${1:-}" == "--reset-cooldown" ]]; then
  RESET_COOLDOWN="true"
fi

POD_IPS="$(kubectl get pods -n "${NAMESPACE}" -l app="${SERVICE}" -o jsonpath='{.items[*].status.podIP}')"

if [[ -n "${POD_IPS}" ]]; then
  kubectl run fault-reset \
    --namespace="${NAMESPACE}" \
    --rm -i --restart=Never \
    --image=curlimages/curl:8.10.1 \
    --command -- sh -c "
      for pod_ip in ${POD_IPS}; do
        curl -sS -X POST http://\${pod_ip}:8080/admin/fault \
          -H 'Content-Type: application/json' \
          -d '{\"mode\": \"NONE\", \"duration_seconds\": 0}'
        echo ''
      done
    "
  echo "[Reset] 全 Pod を正常モード (NONE) に戻しました。"
else
  echo "対象 Pod が見つかりませんでした。"
fi

if [[ "${RESET_COOLDOWN}" == "true" ]]; then
  PROJECT_ARG=()
  if [[ -n "${GOOGLE_CLOUD_PROJECT:-}" ]]; then
    PROJECT_ARG=("--project=${GOOGLE_CLOUD_PROJECT}")
  fi
  gcloud pubsub topics publish "${TOPIC}" \
    "${PROJECT_ARG[@]}" \
    --message='{"incident_id":"ADMIN-RESET","error_code":"ADMIN_COOLDOWN_RESET","summary":"Hands-on drill cooldown reset"}'
  echo "[Reset] 常駐エージェントへクールダウン解除イベント (ADMIN_COOLDOWN_RESET) を送信しました。"
fi

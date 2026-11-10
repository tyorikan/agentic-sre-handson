#!/usr/bin/env bash
# ============================================================================
# 訓練後に各 Pod の擬似障害モードを NONE へ戻すリセットスクリプト
# ============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-sre-demo-app}"
SERVICE="${SERVICE:-order-service}"
POD_IPS="$(kubectl get pods -n "${NAMESPACE}" -l app="${SERVICE}" -o jsonpath='{.items[*].status.podIP}')"

if [[ -z "${POD_IPS}" ]]; then
  echo "対象 Pod が見つかりませんでした。"
  exit 0
fi

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

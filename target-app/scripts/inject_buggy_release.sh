#!/usr/bin/env bash
# ============================================================================
# シナリオ 2 障害注入スクリプト: 既知の設定欠落バグを含む v2 デプロイ (MISSING_CONFIG_BUG)
# 挙動: PAYMENT_CURRENCY_PRECISION が欠落した v2.0.0-buggy をロールアウトし、500 エラーを発生させます。
# 期待される SRE エージェントの判断: AUTO_REMEDIATE（v1.0.0 への自動ロールバック＋回復確認）
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="${NAMESPACE:-sre-demo-app}"
SERVICE="${SERVICE:-order-service}"

if [[ -z "${TARGET_APP_IMAGE_URI:-}" ]]; then
  echo "エラー: 環境変数 TARGET_APP_IMAGE_URI を設定してください（例: asia-northeast1-docker.pkg.dev/<PROJECT>/sre-handson/order-service）" >&2
  exit 1
fi

echo "[Scenario 2] バグ入りリビジョン v2.0.0-buggy を GKE にデプロイします..."
sed "s|TARGET_APP_IMAGE_URI|${TARGET_APP_IMAGE_URI}|g" \
  "${SCRIPT_DIR}/../k8s/deployment-v2-buggy.yaml" | kubectl apply -f -

kubectl rollout status deployment/order-service -n "${NAMESPACE}" --timeout=120s

echo "[Scenario 2] 注文トラフィックを送信し、CONFIG_KEY_ERROR_500 を発生させます..."
kubectl run fault-traffic-v2 \
  --namespace="${NAMESPACE}" \
  --rm -i --restart=Never \
  --image=curlimages/curl:8.10.1 \
  --command -- sh -c "
    for i in \$(seq 1 25); do
      curl -s -o /dev/null -w 'req=%{http_code} ' -X POST http://${SERVICE}.${NAMESPACE}.svc.cluster.local/orders \
        -H 'Content-Type: application/json' \
        -d '{\"item_id\": \"SKU-2002\", \"quantity\": 2, \"amount_jpy\": 9600}'
      sleep 1
    done
    echo ''
  "

echo "[Scenario 2] トラフィック送信完了。常駐 SRE エージェントによる自動ロールバックと Slack 事後レポートを確認してください。"

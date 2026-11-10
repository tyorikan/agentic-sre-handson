#!/usr/bin/env bash
# ============================================================================
# シナリオ 1 障害注入スクリプト: 一時的な下流決済ゲートウェイ瞬断 (TRANSIENT_NET_ERR)
# 挙動: 75 秒間だけ 503 UPSTREAM_CONN_RESET_503 を発生させ、その後自然回復させます。
# 期待される SRE エージェントの判断: OBSERVE（Pod 再起動やロールバックを行わず静観）
# ============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-sre-demo-app}"
SERVICE="${SERVICE:-order-service}"
DURATION_SECONDS="${DURATION_SECONDS:-75}"

echo "[Scenario 1] 一時的なネットワーク瞬断 (${DURATION_SECONDS}秒で自然減衰) を注入します..."

kubectl run fault-injector-transient \
  --namespace="${NAMESPACE}" \
  --rm -i --restart=Never \
  --image=curlimages/curl:8.10.1 \
  --command -- sh -c "
    for pod_ip in \$(nslookup ${SERVICE}.${NAMESPACE}.svc.cluster.local | awk '/^Address: / { print \$2 }' | tail -n +2); do
      curl -sS -X POST http://\${pod_ip}:8080/admin/fault \
        -H 'Content-Type: application/json' \
        -d '{\"mode\": \"TRANSIENT_NET_ERR\", \"duration_seconds\": ${DURATION_SECONDS}}'
      echo ''
    done
    echo 'サンプル注文トラフィックを 20 リクエスト送信してエラーログとメトリクスを発火させます...'
    for i in \$(seq 1 20); do
      curl -s -o /dev/null -w 'req=%{http_code} ' -X POST http://${SERVICE}.${NAMESPACE}.svc.cluster.local/orders \
        -H 'Content-Type: application/json' \
        -d '{\"item_id\": \"SKU-1001\", \"quantity\": 1, \"amount_jpy\": 4800}'
      sleep 1
    done
    echo ''
  "

echo "[Scenario 1] 注入完了。Cloud Logging シンク / Monitoring アラート経由で Pub/Sub に通知されます。"

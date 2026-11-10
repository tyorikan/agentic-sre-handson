#!/usr/bin/env bash
# ============================================================================
# シナリオ 3 障害注入スクリプト: 未知/高リスクの DB コネクション枯渇・ロック競合 (DB_POOL_EXHAUSTED)
# 挙動: DB 最大接続到達とロック待ちエラー (DB_CONN_SLOTS_EXHAUSTED_500) を発生させます。
# 期待される SRE エージェントの判断: REQUIRE_APPROVAL（自動変更を停止し、Slack に Approve/Reject ボタンを送信）
# ============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-sre-demo-app}"
SERVICE="${SERVICE:-order-service}"
DURATION_SECONDS="${DURATION_SECONDS:-600}"

echo "[Scenario 3] 高リスクの DB コネクション枯渇・ロック競合障害を注入します..."

kubectl run fault-injector-db \
  --namespace="${NAMESPACE}" \
  --rm -i --restart=Never \
  --image=curlimages/curl:8.10.1 \
  --command -- sh -c "
    for pod_ip in \$(nslookup ${SERVICE}.${NAMESPACE}.svc.cluster.local | awk '/^Address: / { print \$2 }' | tail -n +2); do
      curl -sS -X POST http://\${pod_ip}:8080/admin/fault \
        -H 'Content-Type: application/json' \
        -d '{\"mode\": \"DB_POOL_EXHAUSTED\", \"duration_seconds\": ${DURATION_SECONDS}}'
      echo ''
    done
    echo 'サンプル注文トラフィックを送信して CRITICAL ログを発火させます...'
    for i in \$(seq 1 20); do
      curl -s -o /dev/null -w 'req=%{http_code} ' -X POST http://${SERVICE}.${NAMESPACE}.svc.cluster.local/orders \
        -H 'Content-Type: application/json' \
        -d '{\"item_id\": \"SKU-3003\", \"quantity\": 1, \"amount_jpy\": 15000}'
      sleep 1
    done
    echo ''
  "

echo "[Scenario 3] 注入完了。Slack に届く調査レポートと [Approve / Reject] ボタンを確認してください。"

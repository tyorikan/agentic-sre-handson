#!/usr/bin/env bash
# ============================================================================
# シナリオ 4 障害注入スクリプト: シード 100 件に未登録の初見インメモリ税率キャッシュ破損
# 挙動:
#   - 1 回目の実行: 既存知識ベースに類似事例がないため REQUIRE_APPROVAL で一時停止。
#                   Slack で [Approve & Execute] を押すとローリング再起動で回復し、
#                   自動で新規 Runbook (AUTO-*) が BigQuery へ登録・AI.EMBED ベクトル化される。
#   - 2 回目の実行: 先ほど自律登録された AUTO-* に高類似度一致し、人間承認なしで AUTO_REMEDIATE 完走。
# ============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-sre-demo-app}"
SERVICE="${SERVICE:-order-service}"
DURATION_SECONDS="${DURATION_SECONDS:-600}"

POD_IPS="$(kubectl get pods -n "${NAMESPACE}" -l app="${SERVICE}" -o jsonpath='{.items[*].status.podIP}')"
if [[ -z "${POD_IPS}" ]]; then
  echo "エラー: Namespace=${NAMESPACE} に稼働中 Pod (${SERVICE}) が見つかりません。" >&2
  exit 1
fi

echo "[Scenario 4] 初見のインメモリ税率テーブル CRC32 破損 (TAX_CACHE_CORRUPTION) を注入します (Pods: ${POD_IPS})..."

kubectl run fault-injector-novel-tax \
  --namespace="${NAMESPACE}" \
  --rm -i --restart=Never \
  --image=curlimages/curl:8.10.1 \
  --command -- sh -c "
    for pod_ip in ${POD_IPS}; do
      curl -sS -X POST http://\${pod_ip}:8080/admin/fault \
        -H 'Content-Type: application/json' \
        -d '{\"mode\": \"TAX_CACHE_CORRUPTION\", \"duration_seconds\": ${DURATION_SECONDS}}'
      echo ''
    done
    echo '注文トラフィックを送信し、初見エラー TAX_RULE_CRC32_MISMATCH_500 を発火させます...'
    for i in \$(seq 1 20); do
      curl -s -o /dev/null -w 'req=%{http_code} ' -X POST http://${SERVICE}.${NAMESPACE}.svc.cluster.local/orders \
        -H 'Content-Type: application/json' \
        -d '{\"item_id\": \"SKU-4004\", \"quantity\": 1, \"amount_jpy\": 5500}'
      sleep 1
    done
    echo ''
  "

echo "[Scenario 4] トラフィック送信完了。1回目は Slack 承認待ち＋自律ナレッジ登録、2回目は自動ローリング再起動を確認してください。"

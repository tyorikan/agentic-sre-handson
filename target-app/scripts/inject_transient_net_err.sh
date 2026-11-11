#!/usr/bin/env bash
# ============================================================================
# シナリオ 1 障害注入スクリプト: 一時的な下流決済ゲートウェイ瞬断 (TRANSIENT_NET_ERR)
# 挙動: 正常ベースライン送信 → 15秒間だけ 503 発生 → 自然回復後の正常トラフィック送信
# 期待される SRE エージェントの判断: OBSERVE（Pod 再起動やロールバックを行わず静観）
# ============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-sre-demo-app}"
SERVICE="${SERVICE:-order-service}"
DURATION_SECONDS="${DURATION_SECONDS:-15}"

POD_IPS="$(kubectl get pods -n "${NAMESPACE}" -l app="${SERVICE}" -o jsonpath='{.items[*].status.podIP}')"
if [[ -z "${POD_IPS}" ]]; then
  echo "エラー: Namespace=${NAMESPACE} に稼働中 Pod (${SERVICE}) が見つかりません。" >&2
  exit 1
fi

echo "[Scenario 1] 一時的なネットワーク瞬断 (${DURATION_SECONDS}秒で自然減衰) のシナリオを開始します (Pods: ${POD_IPS})..."

kubectl run fault-injector-transient \
  --namespace="${NAMESPACE}" \
  --rm -i --restart=Never \
  --image=curlimages/curl:8.10.1 \
  --command -- sh -c "
    echo '1/4 正常ベースライントラフィック (8リクエスト) を送信します...'
    for i in \$(seq 1 8); do
      curl -s -o /dev/null -w 'pre=%{http_code} ' -X POST http://${SERVICE}.${NAMESPACE}.svc.cluster.local/orders \
        -H 'Content-Type: application/json' \
        -d '{\"item_id\": \"SKU-1000\", \"quantity\": 1, \"amount_jpy\": 1200}'
      sleep 1
    done
    echo ''

    echo '2/4 下流決済ゲートウェイ TLS 瞬断 (${DURATION_SECONDS}秒) を注入します...'
    for pod_ip in ${POD_IPS}; do
      curl -sS -X POST http://\${pod_ip}:8080/admin/fault \
        -H 'Content-Type: application/json' \
        -d '{\"mode\": \"TRANSIENT_NET_ERR\", \"duration_seconds\": ${DURATION_SECONDS}}'
      echo ''
    done

    echo '3/4 スパイク中トラフィック (12リクエスト) を送信します...'
    for i in \$(seq 1 12); do
      curl -s -o /dev/null -w 'err=%{http_code} ' -X POST http://${SERVICE}.${NAMESPACE}.svc.cluster.local/orders \
        -H 'Content-Type: application/json' \
        -d '{\"item_id\": \"SKU-1001\", \"quantity\": 1, \"amount_jpy\": 4800}'
      sleep 1
    done
    echo ''

    echo '4/4 障害タイマーの自然終了 (5秒待機) 後、正常回復トラフィック (16リクエスト) を送信します...'
    sleep 5
    for i in \$(seq 1 16); do
      curl -s -o /dev/null -w 'rec=%{http_code} ' -X POST http://${SERVICE}.${NAMESPACE}.svc.cluster.local/orders \
        -H 'Content-Type: application/json' \
        -d '{\"item_id\": \"SKU-1002\", \"quantity\": 1, \"amount_jpy\": 3200}'
      sleep 1
    done
    echo ''
  "

echo "[Scenario 1] 注入＋自然回復トラフィック送信完了。観測ウィンドウ後に OBSERVE 通知が出力されます。"

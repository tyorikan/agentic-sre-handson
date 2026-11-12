#!/usr/bin/env bash
# ============================================================================
# クイックスタートのスクリプトが共用する定義と関数 (quickstart/lib.sh)
# 単体では実行しません。setup.sh / deploy_agent.sh / cleanup.sh から読み込みます。
# ============================================================================

# --- フル版の手順と同じ内容を保つ定義 --------------------------------------
# tests/test_quickstart_consistency.py が、フル版のドキュメントと食い違っていないかを照合します。
# 片方を変えるときは、もう片方も合わせて変えてください。

# 第 0 章 Step 2.2 と第 4 章 5.2 で有効化する API
REQUIRED_APIS=(
  compute.googleapis.com
  container.googleapis.com
  run.googleapis.com
  bigquery.googleapis.com
  bigqueryconnection.googleapis.com
  firestore.googleapis.com
  pubsub.googleapis.com
  monitoring.googleapis.com
  logging.googleapis.com
  aiplatform.googleapis.com
  developerknowledge.googleapis.com
  artifactregistry.googleapis.com
  cloudbuild.googleapis.com
  secretmanager.googleapis.com
  integrations.googleapis.com
)

# 第 0 章 Step 2.3 で 3 つのサービスアカウントへ付与するロール
BUILD_SA_ROLES=(
  roles/storage.objectViewer
  roles/artifactregistry.writer
  roles/logging.logWriter
)
NODE_SA_ROLES=(
  roles/container.defaultNodeServiceAccount
  roles/artifactregistry.reader
)
# 末尾の roles/integrations.integrationInvoker は、第 4 章 5.2 で付与する通知メールの実行権限
AGENT_SA_ROLES=(
  roles/monitoring.viewer
  roles/logging.viewer
  roles/mcp.toolUser
  roles/serviceusage.serviceUsageConsumer
  roles/bigquery.dataEditor
  roles/bigquery.jobUser
  roles/bigquery.connectionUser
  roles/datastore.user
  roles/pubsub.subscriber
  roles/aiplatform.user
  roles/container.clusterViewer
  roles/secretmanager.secretAccessor
  roles/integrations.integrationInvoker
)

# クイックスタートの 1 周で送るメールの通数（テスト 1 通と、訓練 1〜4 の通知 7 通）
QUICKSTART_EMAIL_COUNT=8

# --- 実行環境 ----------------------------------------------------------------
QS_PY="${QS_REPO_ROOT}/.venv/bin/python"
QS_LOG=""
QS_CURRENT_STEP=""
QS_STEP_TITLES=()
QS_STEP_SECONDS=()

# 端末への出力は fd 3 に集約する（各段階のコマンド出力はログファイルへ送るため）
exec 3>&1

qs_die() {
  echo "エラー: $*" >&3
  exit 1
}

# 端末とログの両方へ 1 行出力する
qs_say() {
  echo "$*" >&3
  if [[ -n "${QS_LOG}" ]]; then
    echo "$*" >>"${QS_LOG}"
  fi
}

qs_init_log() {
  local kind="$1"
  QS_LOG="${TMPDIR:-/tmp}/sre-quickstart-${kind}-$(date +%Y%m%d-%H%M%S).log"
  : >"${QS_LOG}"
  echo "詳細なログ: ${QS_LOG}" >&3
}

qs_on_error() {
  local code=$?
  # $(...) の中で失敗した場合は、呼び出し元でもう一度この関数が呼ばれる。案内を二重に出さないよう、ここでは終了だけする
  if ((BASH_SUBSHELL > 0)); then
    exit "${code}"
  fi
  {
    echo ""
    echo "[NG] 失敗した段階: ${QS_CURRENT_STEP:-開始前の確認}（終了コード ${code}）"
    if [[ -n "${QS_LOG}" && -s "${QS_LOG}" ]]; then
      echo "---- ログの末尾 ----"
      tail -n 25 "${QS_LOG}"
      echo "--------------------"
      echo "ログの全文: ${QS_LOG}"
    fi
    echo "原因を解消してから同じコマンドをもう一度実行すると、済んだものを飛ばして続きから進みます。"
  } >&3
  exit "${code}"
}

# 1 つの段階を実行し、所要秒数を記録する。段階の中のコマンド出力はログファイルへ送る。
# 出力が見えない状態で入力待ちになって止まらないよう、標準入力は閉じておく
qs_step() {
  local title="$1"
  shift
  local started=${SECONDS}
  QS_CURRENT_STEP="${title}"
  echo "[$(date +%H:%M:%S)] ${title}" >&3
  printf '\n===== %s =====\n' "${title}" >>"${QS_LOG}"
  "$@" >>"${QS_LOG}" 2>&1 </dev/null
  local took=$((SECONDS - started))
  QS_STEP_TITLES+=("${title}")
  QS_STEP_SECONDS+=("${took}")
  echo "           完了（${took} 秒）" >&3
  QS_CURRENT_STEP=""
}

qs_print_summary() {
  local heading="$1"
  local i total=0
  qs_say ""
  qs_say "${heading}"
  for i in "${!QS_STEP_TITLES[@]}"; do
    qs_say "$(printf '  %4d 秒  %s' "${QS_STEP_SECONDS[$i]}" "${QS_STEP_TITLES[$i]}")"
    total=$((total + QS_STEP_SECONDS[i]))
  done
  qs_say "$(printf '  %4d 秒  合計（約 %d 分）' "${total}" "$(((total + 30) / 60))")"
}

qs_require_commands() {
  local name
  for name in "$@"; do
    command -v "${name}" >/dev/null 2>&1 || qs_die "コマンド ${name} が見つかりません。インストールしてからもう一度実行してください。"
  done
}

# 対象プロジェクトの ID を入力してもらい、一致しなければ何もせずに終了する
qs_confirm_project() {
  local prompt="$1"
  local answer=""
  printf '%s' "${prompt}" >&3
  if ! IFS= read -r answer; then
    answer=""
  fi
  # 入力がターミナルからでない場合は Enter の改行が画面に出ないため、ここで改行する
  if [[ ! -t 0 ]]; then
    echo "" >&3
  fi
  if [[ "${answer}" != "${GOOGLE_CLOUD_PROJECT}" ]]; then
    echo "" >&3
    echo "中止しました。何も変更していません。" >&3
    exit 1
  fi
}

# 指定した回数まで、間隔を空けてコマンドを再試行する: qs_retry <回数> <待機秒> <コマンド...>
qs_retry() {
  local attempts="$1" wait_seconds="$2"
  shift 2
  local n
  for ((n = 1; n <= attempts; n++)); do
    if "$@"; then
      return 0
    fi
    if ((n < attempts)); then
      echo "  再試行します（${n}/${attempts} 回目が失敗。${wait_seconds} 秒待機）"
      sleep "${wait_seconds}"
    fi
  done
  return 1
}

# サービスアカウントへロールを付与する。作成直後は反映待ちで失敗することがあるため再試行する
qs_grant_roles() {
  local member="$1"
  shift
  local role
  for role in "$@"; do
    qs_retry 6 5 gcloud projects add-iam-policy-binding "${GOOGLE_CLOUD_PROJECT}" \
      --member="${member}" \
      --role="${role}" \
      --condition=None \
      --quiet \
      --format=none
    echo "  付与: ${role} -> ${member}"
  done
}

# 付与したロールを取り消す。既に無い場合は何もしない
qs_revoke_roles() {
  local member="$1"
  shift
  local role
  for role in "$@"; do
    gcloud projects remove-iam-policy-binding "${GOOGLE_CLOUD_PROJECT}" \
      --member="${member}" \
      --role="${role}" \
      --condition=None \
      --quiet \
      --format=none >/dev/null 2>&1 || true
    echo "  取り消し: ${role} -> ${member}"
  done
}

# 手元で使う Python スクリプト用の仮想環境を用意する（作成済みで必要なパッケージが入っていれば何もしない）
qs_ensure_venv() {
  if [[ ! -x "${QS_PY}" ]]; then
    python3 -m venv "${QS_REPO_ROOT}/.venv"
  fi
  if ! "${QS_PY}" -c "import dotenv, pydantic, google.cloud.monitoring_v3, google.cloud.logging_v2, google.cloud.firestore, google.cloud.pubsub_v1" >/dev/null 2>&1; then
    "${QS_PY}" -m pip install --quiet --disable-pip-version-check -r "${QS_REPO_ROOT}/quickstart/requirements.txt"
  fi
}

# メール送信用の連携が登録済みかを調べ、結果を QS_EMAIL_STATE（active / absent）へ入れる。
# 調べられなかった場合は 0 以外を返し、呼び出し元の段階を失敗させる
QS_EMAIL_STATE=""
qs_check_email_integration() {
  local code=0
  "${QS_PY}" "${QS_REPO_ROOT}/target-app/scripts/setup_email_notification.py" --status || code=$?
  case "${code}" in
    0) QS_EMAIL_STATE="active" ;;
    4) QS_EMAIL_STATE="absent" ;;
    *)
      echo "メール送信用の連携の状態を確認できませんでした（終了コード ${code}）。"
      return "${code}"
      ;;
  esac
}

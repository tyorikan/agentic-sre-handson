#!/usr/bin/env python3
"""クイックスタートのスクリプトを、クラウドへ接続せずに動かすための差し替えコマンド。

tests/test_quickstart_scripts.py が、gcloud・bq・kubectl・sleep と仮想環境の python の代わりに呼び出します。
実際のコマンドの出力を再現するものではありません。スクリプトの分岐
（作成済みなら飛ばす、確認の入力が違えば何もしない、失敗したら止まる）を確かめるための、最小限の動作だけを持ちます。

使い方:
    fake_cli.py <差し替えるコマンド名> <そのコマンドへの引数...>

環境変数:
    QS_FAKE_STATE_DIR            呼び出しの記録（calls.log）と、リソースの有無の記録を置くディレクトリ
    QS_FAKE_EXISTING             "1" なら、すべてのリソースが作成済みの状態から始める
    QS_FAKE_FAIL                 この文字列を含む呼び出しを失敗させる（例: "builds submit"）
    QS_FAKE_EMAIL_LIMIT_TOO_LOW  "1" なら、通知メールの登録が「送信上限が足りない」（終了コード 3）で終わる
    QS_FAKE_SLACK_SECRETS        "1" なら、Slack のシークレットが登録済みとして扱う
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

STATE_DIR = Path(os.environ["QS_FAKE_STATE_DIR"])
START_WITH_EVERYTHING = os.environ.get("QS_FAKE_EXISTING") == "1"

_CREATE_VERBS = {"create", "create-auto", "deploy"}
_VERBS = _CREATE_VERBS | {"describe", "delete"}
_EMAIL_INTEGRATION = "email integration"


def _read_keys(name: str) -> set[str]:
    path = STATE_DIR / name
    return set(path.read_text(encoding="utf-8").splitlines()) if path.exists() else set()


def _write_keys(name: str, keys: set[str]) -> None:
    (STATE_DIR / name).write_text("".join(f"{key}\n" for key in sorted(keys)), encoding="utf-8")


def _exists(key: str) -> bool:
    """リソースがあるかどうか。作成された、または最初からあって削除されていなければ True。"""
    return key in _read_keys("created") or (START_WITH_EVERYTHING and key not in _read_keys("deleted"))


def _set_exists(key: str, present: bool) -> None:
    created, deleted = _read_keys("created"), _read_keys("deleted")
    (created if present else deleted).add(key)
    (deleted if present else created).discard(key)
    _write_keys("created", created)
    _write_keys("deleted", deleted)


def _flag_value(flags: list[str], name: str) -> str:
    for flag in flags:
        if flag.startswith(f"{name}="):
            return flag.split("=", 1)[1]
    return ""


def run_gcloud(args: list[str]) -> int:
    words = [arg for arg in args if not arg.startswith("-")]
    flags = [arg for arg in args if arg.startswith("-")]

    if "--help" in flags:
        return 0
    if words[:3] == ["config", "get-value", "account"]:
        print("tester@example.com")
        return 0
    if words[:2] == ["projects", "describe"]:
        print(words[2])
        return 0
    if words[:2] == ["secrets", "describe"]:
        return 0 if os.environ.get("QS_FAKE_SLACK_SECRETS") == "1" else 1
    if words[:2] == ["logging", "read"]:
        print("False\tTrue\tTrue")
        return 0
    if words[:3] == ["compute", "firewall-rules", "list"]:
        if _exists("compute firewall-rules|gke-leftover-rule"):
            print("gke-leftover-rule")
        return 0
    if words[:2] == ["builds", "submit"]:
        image = _flag_value(flags, "--substitutions").split("_IMAGE_URI=", 1)[1]
        _set_exists(f"artifacts docker images|{image}", True)
        return 0

    verb_at = next((index for index, word in enumerate(words) if word in _VERBS), None)
    if verb_at is None:
        # API の有効化、ロールの付与と取り消し、kubectl の認証情報の取得など
        return 0
    verb = words[verb_at]
    kind = " ".join(words[:verb_at])
    name = words[verb_at + 1] if len(words) > verb_at + 1 else ""
    if kind == "iam service-accounts":
        # 作成は名前で、参照と削除はメールアドレスで指定されるため、名前にそろえる
        name = name.split("@", 1)[0]
    key = f"{kind}|{name}"

    if verb in _CREATE_VERBS:
        _set_exists(key, True)
        return 0
    if verb == "delete":
        _set_exists(key, False)
        return 0
    if not _exists(key):
        print(f"ERROR: NOT_FOUND: {key}", file=sys.stderr)
        return 1
    output = {
        "value(status)": "RUNNING",
        "value(uniqueId)": "100000000000000000001",
        "value(writerIdentity)": "serviceAccount:service-1@gcp-sa-logging.iam.gserviceaccount.com",
    }.get(_flag_value(flags, "--format"))
    if output:
        print(output)
    return 0


def run_bq(args: list[str]) -> int:
    words = [arg for arg in args if not arg.startswith("-")]
    flags = [arg for arg in args if arg.startswith("-")]
    command = words[0] if words else ""
    key = "bq connection" if "--connection" in flags else "bq dataset"

    if command == "show":
        if not _exists(key):
            print(f"BigQuery error: Not found: {key}", file=sys.stderr)
            return 1
        if "--format=json" in flags:
            print(json.dumps({"cloudResource": {"serviceAccountId": "bqcx-1@gcp-sa-bigquery-condel.iam.gserviceaccount.com"}}))
        return 0
    if command == "mk":
        _set_exists("bq connection", True)
    elif command == "rm":
        _set_exists(key, False)
    elif command == "load":
        _set_exists("bq dataset", True)
    elif command == "query":
        if "--format=csv" in flags:
            # 件数、埋め込みが完了した件数、埋め込みに失敗した件数
            print("f0_,f1_,f2_\n100,100,0")
        elif not sys.stdin.isatty():
            sys.stdin.read()
    return 0


def run_venv_python(args: list[str]) -> int:
    if not any(arg.endswith("setup_email_notification.py") for arg in args):
        return 0
    if "--status" in args:
        return 0 if _exists(_EMAIL_INTEGRATION) else 4
    if "--delete" in args:
        _set_exists(_EMAIL_INTEGRATION, False)
        return 0
    if os.environ.get("QS_FAKE_EMAIL_LIMIT_TOO_LOW") == "1":
        return 3
    _set_exists(_EMAIL_INTEGRATION, True)
    return 0


def main() -> int:
    tool, args = sys.argv[1], sys.argv[2:]
    line = " ".join([tool, *args])
    with (STATE_DIR / "calls.log").open("a", encoding="utf-8") as log:
        log.write(line.replace("\n", " ") + "\n")

    fail_on = os.environ.get("QS_FAKE_FAIL", "")
    if fail_on and fail_on in line:
        print(f"ERROR: 差し替えコマンドが失敗を再現しました: {fail_on}", file=sys.stderr)
        return 1
    if tool == "gcloud":
        return run_gcloud(args)
    if tool == "bq":
        return run_bq(args)
    if tool == "python":
        return run_venv_python(args)
    if tool == "kubectl" and "-" in args:
        # kubectl apply -f - は、パイプで渡されたマニフェストを最後まで読む
        sys.stdin.read()
    # kubectl・gke-gcloud-auth-plugin・sleep は、呼び出しを記録するだけで成功を返す
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

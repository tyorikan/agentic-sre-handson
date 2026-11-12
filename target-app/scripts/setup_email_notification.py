#!/usr/bin/env python3
"""通知メールを送るための Application Integration の連携を登録・削除するスクリプト（Slack を使えない場合の代替ルート）。

登録するもの:
  - メール送信用の連携 1 つ（既定名 sre-notify-email）。API トリガー 1 つとメール送信タスク 1 つで構成する。
  - 宛先は連携の定義に固定する。エージェントが実行時に渡せるのは件名と本文だけ。

行うこと:
  1. 対象リージョンで Application Integration が有効化されていなければ有効化する。
  2. 連携を登録して公開する。登録済みの定義と同じなら何もしない（再実行しても重複しない）。
  3. このプロジェクトの 1 日あたりのメール送信上限を表示する。
  4. --send-test を付けた場合は、テストメールを 1 通送る。

使い方:
  # 登録（宛先はカンマ区切りで 30 件まで）
  python3 ./target-app/scripts/setup_email_notification.py --to you@example.com --send-test

  # 削除（第 6 章の削除手順で使用）
  python3 ./target-app/scripts/setup_email_notification.py --delete

  # 登録済みかどうかだけを調べる（何も変更しない。登録済みなら終了コード 0、未登録なら 4）
  python3 ./target-app/scripts/setup_email_notification.py --status

  # 1 日の送信上限が指定した通数に足りない場合は、何も登録せずに終了コード 3 で終わる
  python3 ./target-app/scripts/setup_email_notification.py --min-daily-limit 8

IAM について:
  このスクリプト自身は IAM ポリシーを書き換えません。エージェントのサービスアカウントへ連携の実行権限
  （roles/integrations.integrationInvoker）を付与する手順は、ドキュメントを参照してください。
  リージョンを有効化すると、Google Cloud 側でサービス エージェントへのロール付与と、
  ExecuteConnection という連携の作成が自動的に行われます。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "sre-agent"))

from agent_app.email_notifier import EMAIL_BODY_PARAM, EMAIL_SUBJECT_PARAM, EMAIL_TRIGGER_ID  # noqa: E402

DEFAULT_INTEGRATION_NAME = "sre-notify-email"
MAX_RECIPIENTS = 30
# 訓練 1 周で約 9 通を送るため、これより上限が低いプロジェクトでは通知が途中で止まる可能性がある
LOW_DAILY_LIMIT = 20
EMAIL_QUOTA_METRIC = "integrations.googleapis.com/email_sent_count"

# 呼び出し元のスクリプトが結果を区別できるよう、失敗（1）とは別の終了コードを使う
EXIT_DAILY_LIMIT_TOO_LOW = 3
EXIT_NOT_REGISTERED = 4

_EMAIL_PATTERN = re.compile(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+")
_SEND_TASK_ID = "1"


class IntegrationApiError(RuntimeError):
    """Application Integration の API 呼び出しが失敗したことを表す例外。"""


def parse_recipients(raw: str) -> list[str]:
    """カンマ区切りの宛先を検証し、重複を除いて返す。"""
    recipients: list[str] = []
    for item in raw.split(","):
        address = item.strip()
        if not address:
            continue
        if not _EMAIL_PATTERN.fullmatch(address):
            raise ValueError(f"メールアドレスの形式が正しくありません: {address}")
        if address.lower() not in (existing.lower() for existing in recipients):
            recipients.append(address)
    if not recipients:
        raise ValueError("宛先がありません。--to または NOTIFY_EMAIL_TO でメールアドレスを指定してください。")
    if len(recipients) > MAX_RECIPIENTS:
        raise ValueError(f"宛先は {MAX_RECIPIENTS} 件までです（指定: {len(recipients)} 件）。")
    return recipients


def build_integration_version(recipients: list[str]) -> dict[str, Any]:
    """メール送信用の連携の定義を組み立てる。

    宛先はこの定義に固定する。呼び出し側（エージェント）が渡せるのは件名と本文だけなので、
    エージェントが誤動作しても決められた宛先以外へはメールを送れない。
    """

    def _strings(key: str, values: list[str] | None = None) -> dict[str, Any]:
        return {"key": key, "value": {"stringArray": {"stringValues": values} if values else {}}}

    def _string(key: str, value: str) -> dict[str, Any]:
        return {"key": key, "value": {"stringValue": value}}

    return {
        "description": "SRE エージェントの通知メールを送る連携。宛先はこの定義に固定し、呼び出し側は件名と本文だけを渡す。",
        "triggerConfigs": [
            {
                "label": "API Trigger",
                "triggerType": "API",
                "triggerNumber": "1",
                "triggerId": EMAIL_TRIGGER_ID,
                "startTasks": [{"taskId": _SEND_TASK_ID}],
                "properties": {"Trigger name": EMAIL_TRIGGER_ID.split("/", 1)[1]},
            }
        ],
        "taskConfigs": [
            {
                "task": "EmailTask",
                "taskId": _SEND_TASK_ID,
                "displayName": "Send Email",
                "parameters": {
                    "To": _strings("To", recipients),
                    "Cc": _strings("Cc"),
                    "Bcc": _strings("Bcc"),
                    "AttachmentPath": _strings("AttachmentPath"),
                    "Subject": _string("Subject", f"${EMAIL_SUBJECT_PARAM}$"),
                    "BodyFormat": _string("BodyFormat", "text"),
                    "TextBody": _string("TextBody", f"${EMAIL_BODY_PARAM}$"),
                    "EmailConfigInput": {
                        "key": "EmailConfigInput",
                        "value": {
                            "jsonValue": '{\n  "@type": "type.googleapis.com/enterprise.crm.eventbus.proto.EmailConfig"\n}'
                        },
                    },
                    "ThrowNoRequiredInputException": {
                        "key": "ThrowNoRequiredInputException",
                        "value": {"booleanValue": True},
                    },
                },
                "taskExecutionStrategy": "WHEN_ALL_SUCCEED",
                "externalTaskType": "NORMAL_TASK",
            }
        ],
        "integrationParameters": [
            {"key": key, "dataType": "STRING_VALUE", "displayName": key, "inputOutputType": "IN"}
            for key in (EMAIL_SUBJECT_PARAM, EMAIL_BODY_PARAM)
        ],
    }


def definition_signature(version: dict[str, Any]) -> dict[str, Any]:
    """連携の定義のうち、このスクリプトが決めている部分だけを取り出す。

    登録済みの定義には Google Cloud 側で項目が追加されるため、全体は比べられない。
    宛先・トリガー・件名と本文の受け渡しが同じなら、同じ定義として扱う。
    """
    triggers = [
        (
            trigger.get("triggerType", ""),
            trigger.get("triggerId", ""),
            tuple(start.get("taskId", "") for start in trigger.get("startTasks", [])),
        )
        for trigger in version.get("triggerConfigs", [])
    ]
    tasks = []
    for task in version.get("taskConfigs", []):
        parameters = task.get("parameters", {})

        def _strings(key: str, parameters: dict[str, Any] = parameters) -> tuple[str, ...]:
            values = parameters.get(key, {}).get("value", {}).get("stringArray", {}).get("stringValues", [])
            return tuple(sorted(values))

        def _string(key: str, parameters: dict[str, Any] = parameters) -> str:
            return str(parameters.get(key, {}).get("value", {}).get("stringValue", ""))

        tasks.append(
            {
                "task": task.get("task", ""),
                "task_id": task.get("taskId", ""),
                "to": _strings("To"),
                "cc": _strings("Cc"),
                "bcc": _strings("Bcc"),
                "subject": _string("Subject"),
                "body_format": _string("BodyFormat"),
                "text_body": _string("TextBody"),
            }
        )
    inputs = sorted(
        parameter.get("key", "")
        for parameter in version.get("integrationParameters", [])
        if parameter.get("inputOutputType") == "IN"
    )
    return {"triggers": triggers, "tasks": tasks, "inputs": inputs}


class IntegrationApi:
    """Application Integration の REST API を呼び出す薄い窓口（テストでは session を差し替える）。"""

    def __init__(self, session: Any, project: str, location: str) -> None:
        self._session = session
        self._root = f"https://{location}-integrations.googleapis.com/v1"
        self._parent = f"projects/{project}/locations/{location}"

    def _call(
        self,
        method: str,
        resource: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        allow_not_found: bool = False,
    ) -> dict[str, Any] | None:
        response = self._session.request(
            method,
            f"{self._root}/{resource}",
            json=json_body,
            params=params,
            timeout=60,
        )
        if response.status_code == 404 and allow_not_found:
            return None
        if response.status_code >= 300:
            raise IntegrationApiError(
                f"{method} {resource} が失敗しました (HTTP {response.status_code}): {response.text[:500]}"
            )
        return response.json() if response.content else {}

    def is_region_provisioned(self) -> bool:
        return self._call("GET", f"{self._parent}/clients", allow_not_found=True) is not None

    def provision_region(self) -> None:
        self._call("POST", f"{self._parent}/clients:provision", json_body={"createSampleWorkflows": False})

    def list_versions(self, name: str, *, active_only: bool = False) -> list[dict[str, Any]]:
        params = {"filter": "state=ACTIVE"} if active_only else None
        listed = self._call("GET", f"{self._parent}/integrations/{name}/versions", params=params) or {}
        return list(listed.get("integrationVersions", []))

    def get_version(self, version_name: str) -> dict[str, Any]:
        return self._call("GET", version_name) or {}

    def create_version(self, name: str, body: dict[str, Any], *, first: bool) -> dict[str, Any]:
        params = {"newIntegration": "true"} if first else None
        return self._call("POST", f"{self._parent}/integrations/{name}/versions", json_body=body, params=params) or {}

    def publish_version(self, version_name: str) -> None:
        self._call("POST", f"{version_name}:publish", json_body={})

    def execute(self, name: str, subject: str, body: str) -> str:
        executed = self._call(
            "POST",
            f"{self._parent}/integrations/{name}:execute",
            json_body={
                "triggerId": EMAIL_TRIGGER_ID,
                "inputParameters": {
                    EMAIL_SUBJECT_PARAM: {"stringValue": subject},
                    EMAIL_BODY_PARAM: {"stringValue": body},
                },
            },
        ) or {}
        return str(executed.get("executionId", ""))

    def delete_integration(self, name: str) -> None:
        self._call("DELETE", f"{self._parent}/integrations/{name}")


def ensure_region_provisioned(api: IntegrationApi) -> bool:
    """リージョンが未有効化なら有効化する。新しく有効化した場合は True を返す。"""
    if api.is_region_provisioned():
        return False
    api.provision_region()
    return True


def upsert_integration(api: IntegrationApi, name: str, recipients: list[str]) -> str:
    """連携を登録して公開する。結果を created / updated / unchanged のいずれかで返す。

    公開中の定義が同じなら何もしない。違う場合だけ新しいバージョンを作って公開する。
    """
    desired = build_integration_version(recipients)
    active_versions = api.list_versions(name, active_only=True)
    if active_versions:
        active = api.get_version(active_versions[0]["name"])
        if definition_signature(active) == definition_signature(desired):
            return "unchanged"
    exists = bool(active_versions) or bool(api.list_versions(name))
    created = api.create_version(name, desired, first=not exists)
    api.publish_version(created["name"])
    return "updated" if exists else "created"


def is_integration_active(api: IntegrationApi, name: str) -> bool:
    """メール送信用の連携が公開済みで、実行できる状態かどうかを返す（何も変更しない）。"""
    return api.is_region_provisioned() and bool(api.list_versions(name, active_only=True))


def remove_integration(api: IntegrationApi, name: str) -> bool:
    """このスクリプトが登録した連携だけを削除する。存在しなければ False を返す。"""
    if not api.list_versions(name):
        return False
    api.delete_integration(name)
    return True


def read_email_daily_limit(session: Any, project: str) -> int | None:
    """このプロジェクトの 1 日あたりのメール送信上限を読み取る。確認できなければ None を返す。"""
    url = (
        f"https://serviceusage.googleapis.com/v1beta1/projects/{project}"
        "/services/integrations.googleapis.com/consumerQuotaMetrics"
    )
    params: dict[str, str] = {"pageSize": "100"}
    try:
        for _ in range(5):
            response = session.request("GET", url, params=params, timeout=60)
            if response.status_code >= 300:
                return None
            payload = response.json()
            for metric in payload.get("metrics", []):
                if metric.get("metric") != EMAIL_QUOTA_METRIC:
                    continue
                limits = [
                    int(bucket["effectiveLimit"])
                    for limit in metric.get("consumerQuotaLimits", [])
                    for bucket in limit.get("quotaBuckets", [])
                    if "effectiveLimit" in bucket
                ]
                return min(limits) if limits else None
            token = payload.get("nextPageToken")
            if not token:
                return None
            params = {"pageSize": "100", "pageToken": token}
    except Exception:  # noqa: BLE001 - 上限の表示は補助情報なので、読み取れなくてもセットアップは続ける
        return None
    return None


def describe_daily_limit(limit: int | None) -> str:
    """メール送信上限の表示文を作る（上限はプロジェクトによって異なる）。"""
    if limit is None:
        return "[INFO] このプロジェクトのメール送信上限は確認できませんでした。コンソールの割り当てページで確認できます。"
    if limit < LOW_DAILY_LIMIT:
        return (
            f"[WARN] このプロジェクトのメール送信上限は 1 日 {limit:,} 通です。"
            "訓練 1 周で約 9 通を送るため、通知が途中で届かなくなる可能性があります。"
        )
    return f"[INFO] このプロジェクトのメール送信上限は 1 日 {limit:,} 通です。"


def _build_session(project: str) -> Any:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
        quota_project_id=project,
    )
    return AuthorizedSession(credentials)


def main(argv: list[str] | None = None, *, session: Any | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env")

    parser = argparse.ArgumentParser(description="通知メール用の Application Integration 連携を登録・削除する")
    parser.add_argument("--project", default=os.getenv("GOOGLE_CLOUD_PROJECT", ""), help="Google Cloud プロジェクト ID")
    parser.add_argument(
        "--location",
        default=os.getenv("EMAIL_NOTIFY_LOCATION") or os.getenv("GOOGLE_CLOUD_REGION", "asia-northeast1"),
        help="Application Integration のリージョン",
    )
    parser.add_argument(
        "--name",
        default=os.getenv("EMAIL_NOTIFY_INTEGRATION") or DEFAULT_INTEGRATION_NAME,
        help="登録する連携の名前",
    )
    parser.add_argument("--to", default=os.getenv("NOTIFY_EMAIL_TO", ""), help="通知メールの宛先（カンマ区切り）")
    parser.add_argument("--send-test", action="store_true", help="登録後にテストメールを 1 通送る")
    parser.add_argument("--delete", action="store_true", help="このスクリプトが登録した連携を削除する")
    parser.add_argument(
        "--status",
        action="store_true",
        help=f"登録済みかどうかだけを調べる。登録済みなら終了コード 0、未登録なら {EXIT_NOT_REGISTERED}",
    )
    parser.add_argument(
        "--min-daily-limit",
        type=int,
        default=0,
        help=f"1 日の送信上限がこの通数に足りない場合は、何も登録せずに終了コード {EXIT_DAILY_LIMIT_TOO_LOW} で終わる",
    )
    args = parser.parse_args(argv)

    if not args.project or args.project == "your-sandbox-project-id":
        print("エラー: GOOGLE_CLOUD_PROJECT 環境変数または --project を設定してください。", file=sys.stderr)
        return 1

    try:
        recipients = [] if (args.delete or args.status) else parse_recipients(args.to)
    except ValueError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1

    try:
        active_session = session or _build_session(args.project)
        api = IntegrationApi(active_session, args.project, args.location)

        if args.status:
            if is_integration_active(api, args.name):
                print(f"[OK] メール送信用の連携は登録済みです: {args.name}")
                return 0
            print(f"[INFO] メール送信用の連携は未登録です: {args.name}")
            return EXIT_NOT_REGISTERED

        if args.delete:
            if not api.is_region_provisioned():
                print(f"[SKIP] {args.location} では Application Integration が有効化されていません。")
                return 0
            if remove_integration(api, args.name):
                print(f"[DELETED] メール送信用の連携を削除しました: {args.name}")
            else:
                print(f"[SKIP] メール送信用の連携は既に存在しません: {args.name}")
            print("[INFO] リージョンの有効化と、有効化時に自動作成された連携 ExecuteConnection は残ります。")
            return 0

        daily_limit = read_email_daily_limit(active_session, args.project)
        if args.min_daily_limit > 0 and daily_limit is not None and daily_limit < args.min_daily_limit:
            print(
                f"エラー: このプロジェクトのメール送信上限は 1 日 {daily_limit:,} 通で、"
                f"必要な {args.min_daily_limit:,} 通に足りません。何も登録していません。",
                file=sys.stderr,
            )
            return EXIT_DAILY_LIMIT_TOO_LOW

        if ensure_region_provisioned(api):
            print(f"[OK] {args.location} で Application Integration を有効化しました。")
            print(
                "[INFO] 有効化に伴い、Google Cloud がサービス エージェントへのロール付与と、"
                "連携 ExecuteConnection の作成を自動で行います。"
            )
        else:
            print(f"[OK] {args.location} の Application Integration は有効化済みです。")

        result = upsert_integration(api, args.name, recipients)
        verb = {
            "created": "登録して公開しました",
            "updated": "新しい定義で公開し直しました",
            "unchanged": "登録済みの定義と同じため、変更していません",
        }[result]
        print(f"[OK] メール送信用の連携: {args.name} — {verb}")
        print(f"     宛先（連携の定義に固定）: {', '.join(recipients)}")

        print(describe_daily_limit(daily_limit))

        if args.send_test:
            sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            api.execute(
                args.name,
                "[SRE Agent] テスト: 通知メールの設定確認",
                "通知メールの設定を確認するためのテストです。\n"
                f"このメールが届いていれば、連携 {args.name} から決められた宛先へ送信できています。\n\n"
                f"送信日時: {sent_at}\n\n"
                "この通知への操作は不要です。",
            )
            print("[OK] テストメールを送信しました。届かない場合は迷惑メールフォルダも確認してください。")
    except IntegrationApiError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - 認証情報の未設定などを、原因が分かる形で伝える
        print(
            f"エラー: セットアップを完了できませんでした。gcloud auth application-default login の実行状況と権限を確認してください: {exc}",
            file=sys.stderr,
        )
        return 1

    print("\n次に行うこと:")
    print(f'  1. .env に EMAIL_NOTIFY_INTEGRATION="{args.name}" を設定する')
    print("  2. エージェントのサービスアカウントへ roles/integrations.integrationInvoker を付与する")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

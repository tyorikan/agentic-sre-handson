"""クイックスタートのスクリプト（quickstart/）の分岐を、クラウドへ接続せずに確かめるテスト。

gcloud・bq・kubectl と仮想環境の python を、tests/quickstart_fakes/fake_cli.py に差し替えて実行します。

確かめること:
  - 確認の入力が一致しない場合と --plan では、何も変更しない
  - 何も無い状態からは必要なものをすべて作り、作成済みの状態からは作り直さない
  - 途中で失敗したら止まり、再実行すると続きから進む
  - 通知と承認の経路を、使えるものに合わせて切り替える
  - 後片付けは課金額の大きいものから削除し、既に無いものは飛ばす

確かめられないこと:
  差し替えコマンドは、実際のコマンドの挙動（権限の反映待ち、API の応答、所要時間）を再現しません。
  実際の Google Cloud での確認は、docs/quickstart.md の手順を通して行います。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_CLI = REPO_ROOT / "tests" / "quickstart_fakes" / "fake_cli.py"
PROJECT = "qs-test-project"
OPERATOR_EMAIL = "operator@example.com"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash が必要です")

_GCLOUD_CHANGE_WORDS = {
    "create",
    "create-auto",
    "delete",
    "deploy",
    "enable",
    "submit",
    "add-iam-policy-binding",
    "remove-iam-policy-binding",
    "get-credentials",
}


def _is_change(call: str) -> bool:
    """差し替えコマンドへの呼び出しが、環境を変更するものかどうか。"""
    tool, _, rest = call.partition(" ")
    words = rest.split()
    if tool == "gcloud":
        return bool(_GCLOUD_CHANGE_WORDS & set(words))
    if tool == "bq":
        return words[0] != "show"
    if tool == "kubectl":
        return True
    if tool == "python":
        if "setup_slo_monitoring.py" in rest:
            return True
        return "setup_email_notification.py" in rest and "--status" not in words
    return False


class Sandbox:
    """スクリプトと差し替えコマンドを置いた、使い捨ての作業場所。"""

    def __init__(self, root: Path) -> None:
        self.repo = root / "repo"
        self.state = root / "state"
        self._bin = root / "bin"
        self._tmp = root / "tmp"
        self._home = root / "home"
        for directory in (self.repo, self.state, self._bin, self._tmp, self._home):
            directory.mkdir(parents=True)

        shutil.copytree(REPO_ROOT / "quickstart", self.repo / "quickstart")
        shutil.copytree(REPO_ROOT / "target-app" / "k8s", self.repo / "target-app" / "k8s")
        shutil.copytree(REPO_ROOT / "bigquery", self.repo / "bigquery")
        shutil.copy(REPO_ROOT / ".env.example", self.repo / ".env")
        # 手順書どおり、.env の 2 項目だけを書き換える
        self.set_env("GOOGLE_CLOUD_PROJECT", PROJECT)
        self.set_env("NOTIFY_EMAIL_TO", OPERATOR_EMAIL)

        for tool in ("gcloud", "bq", "kubectl", "gke-gcloud-auth-plugin", "sleep"):
            self._install_fake(self._bin / tool, tool)
        self._install_fake(self.repo / ".venv" / "bin" / "python", "python")

    @staticmethod
    def _install_fake(path: Path, tool: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLI}" {tool} "$@"\n', encoding="utf-8")
        path.chmod(0o755)

    def set_env(self, name: str, value: str) -> None:
        """.env の 1 項目を書き換える。"""
        path = self.repo / ".env"
        text, count = re.subn(rf'^{name}=".*"$', f'{name}="{value}"', path.read_text(encoding="utf-8"), flags=re.MULTILINE)
        assert count == 1, f".env に {name} の行が 1 つだけあるはずです"
        path.write_text(text, encoding="utf-8")

    def run(self, script: str, *args: str, stdin: str = "", **fake_options: str) -> subprocess.CompletedProcess[str]:
        """スクリプトを実行する。fake_options は差し替えコマンドへ渡す設定（QS_FAKE_ を除いた名前）。"""
        env = {
            "PATH": f"{self._bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self._home),
            "TMPDIR": str(self._tmp),
            "QS_FAKE_STATE_DIR": str(self.state),
            **{f"QS_FAKE_{name.upper()}": value for name, value in fake_options.items()},
        }
        return subprocess.run(
            ["bash", str(self.repo / "quickstart" / script), *args],
            input=stdin,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )

    def calls(self) -> list[str]:
        """差し替えコマンドへの呼び出しを、実行された順に返す。"""
        log = self.state / "calls.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def changes(self, since: int = 0) -> list[str]:
        return [call for call in self.calls()[since:] if _is_change(call)]


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def _positions(calls: list[str], *fragments: str) -> list[int]:
    """各文字列を含む最初の呼び出しの位置を返す（見つからなければ失敗）。"""
    positions = []
    for fragment in fragments:
        found = [index for index, call in enumerate(calls) if fragment in call]
        assert found, f"「{fragment}」を含む呼び出しがありません"
        positions.append(found[0])
    return positions


def _count(calls: list[str], fragment: str) -> int:
    return sum(1 for call in calls if fragment in call)


@pytest.mark.parametrize("script", ["setup.sh", "cleanup.sh"])
def test_nothing_changes_without_the_exact_project_id(sandbox: Sandbox, script: str) -> None:
    """--plan のときと、確認で入力したプロジェクト ID が一致しないときは、何も変更しないこと。"""
    planned = sandbox.run(script, "--plan", existing="1")
    assert planned.returncode == 0, planned.stdout + planned.stderr
    assert f": {PROJECT}" in planned.stdout
    assert sandbox.changes() == []

    for answer in ("another-project\n", f"{PROJECT}-2\n", "y\n", ""):
        refused = sandbox.run(script, stdin=answer, existing="1")
        assert refused.returncode == 1
        assert "中止しました。何も変更していません。" in refused.stdout
    assert sandbox.changes() == []

    unknown_option = sandbox.run(script, "--force", stdin=f"{PROJECT}\n", existing="1")
    assert unknown_option.returncode == 1
    assert "使い方" in unknown_option.stdout
    assert sandbox.changes() == []


def test_setup_builds_everything_once_and_skips_it_on_rerun(sandbox: Sandbox) -> None:
    """何も無い状態からは全体を構築し、もう一度実行しても作り直さないこと。"""
    first = sandbox.run("setup.sh", stdin=f"{PROJECT}\n")
    assert first.returncode == 0, first.stdout + first.stderr
    assert "環境構築が完了しました" in first.stdout
    assert f"通知メール: 設定済みです（宛先: {OPERATOR_EMAIL}）" in first.stdout
    assert "次の手順: ./quickstart/deploy_agent.sh" in first.stdout

    calls = sandbox.calls()
    order = _positions(
        calls,
        "gcloud services enable",
        "gcloud firestore databases create",
        "gcloud iam service-accounts create sre-cloudbuild-sa",
        "gcloud compute networks create",
        "gcloud container clusters create-auto",
        "setup_email_notification.py --send-test --min-daily-limit 8",
        "gcloud builds submit ./target-app",
        "gcloud builds submit . ",
        "bq mk --connection",
        "bq load",
        "gcloud pubsub topics create sre-alert-stream",
        "gcloud pubsub topics create sre-approval-decisions",
        "gcloud logging sinks create",
        "gcloud container clusters get-credentials",
        "kubectl apply",
        "setup_slo_monitoring.py",
    )
    assert order == sorted(order)
    # クラスタの作成は待たずに始め、他の段階を進めてから完了を待つ
    assert "--async" in calls[order[4]]
    assert _count(calls, "gcloud iam service-accounts create") == 3
    # サービスアカウント 3 つへの 18 個と、BigQuery の接続用サービスアカウントへの 1 個
    assert _count(calls, "gcloud projects add-iam-policy-binding") == 19
    assert _count(calls, "kubectl apply") == 3

    checkpoint = len(calls)
    second = sandbox.run("setup.sh", stdin=f"{PROJECT}\n")
    assert second.returncode == 0, second.stdout + second.stderr
    assert "環境構築が完了しました" in second.stdout
    repeated = sandbox.calls()[checkpoint:]
    for fragment in (" create", "create-auto", "builds submit", "bq mk", "--send-test"):
        assert _count(repeated, fragment) == 0, fragment
    # 登録済みの通知メールは、宛先の変更だけを反映する（テストメールは送らない）
    assert _count(repeated, "setup_email_notification.py --min-daily-limit 8") == 1


def test_setup_stops_at_a_failed_step_and_resumes_on_rerun(sandbox: Sandbox) -> None:
    """途中の段階が失敗したら止まって原因の場所を示し、再実行すると続きから進むこと。"""
    failed = sandbox.run("setup.sh", stdin=f"{PROJECT}\n", fail="builds submit")
    assert failed.returncode != 0
    assert failed.stdout.count("[NG] 失敗した段階:") == 1
    assert "[NG] 失敗した段階:  8/13 コンテナイメージをビルド" in failed.stdout
    assert "同じコマンドをもう一度実行すると" in failed.stdout
    assert "環境構築が完了しました" not in failed.stdout

    calls = sandbox.calls()
    # ビルドは 3 回まで試し、失敗したら後ろの段階へ進まない
    assert _count(calls, "gcloud builds submit ./target-app") == 3
    assert _count(calls, "gcloud builds submit . ") == 0
    assert _count(calls, "bq ") == 0
    assert _count(calls, "kubectl") == 0

    resumed = sandbox.run("setup.sh", stdin=f"{PROJECT}\n")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert "環境構築が完了しました" in resumed.stdout
    calls = sandbox.calls()
    # 1 回目で済んだものは作り直さない
    assert _count(calls, "gcloud iam service-accounts create") == 3
    assert _count(calls, "gcloud container clusters create-auto") == 1
    assert _count(calls, "--send-test") == 1
    assert _count(calls, "gcloud builds submit ./target-app") == 4
    assert _count(calls, "gcloud builds submit . ") == 1


def test_setup_continues_without_email_when_the_daily_limit_is_too_low(sandbox: Sandbox) -> None:
    """メールの送信上限が足りないプロジェクトでは、メールを省いて構築を続け、通知の経路が無ければデプロイしないこと。"""
    completed = sandbox.run("setup.sh", stdin=f"{PROJECT}\n", email_limit_too_low="1")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "通知メールの設定を省きました" in completed.stdout
    assert "フル版 第 4 章 5.1 の手順で Slack を準備" in completed.stdout
    assert "通知メール: このプロジェクトでは使えません" in completed.stdout
    assert "環境構築が完了しました" in completed.stdout

    deployed = sandbox.run("deploy_agent.sh")
    assert deployed.returncode != 0
    assert "通知の経路がありません" in deployed.stdout
    assert _count(sandbox.calls(), "gcloud beta run instances deploy") == 0


def _deploy_call(sandbox: Sandbox) -> str:
    deploys = [call for call in sandbox.calls() if "gcloud beta run instances deploy" in call]
    assert deploys, "デプロイのコマンドが実行されていません"
    return deploys[-1]


def test_deploy_uses_email_and_approval_command_by_default(sandbox: Sandbox) -> None:
    """Slack を設定していなければ、メール通知と承認コマンドだけを有効にしてデプロイすること。"""
    sandbox.set_env("SLACK_BOT_TOKEN", "dummy-bot-token-must-not-leak")
    assert sandbox.run("setup.sh", stdin=f"{PROJECT}\n").returncode == 0

    deployed = sandbox.run("deploy_agent.sh")
    assert deployed.returncode == 0, deployed.stdout + deployed.stderr
    assert "メール通知   : 有効" in deployed.stdout
    assert "承認コマンド : 有効" in deployed.stdout
    assert "Slack        : 無効" in deployed.stdout

    call = _deploy_call(sandbox)
    assert "@EMAIL_NOTIFY_INTEGRATION=sre-notify-email@" in call
    assert call.split("@")[-1].startswith("APPROVAL_PUBSUB_SUBSCRIPTION=sre-approval-decisions-sub ")
    assert "@SLACK_ALERT_CHANNEL_ID=@SLACK_ALLOWED_APPROVER_IDS=@" in call
    assert "@ALLOW_DRILL_COOLDOWN_RESET=true@" in call
    for flag in ("--ingress=internal", "--no-default-url", "--sandbox-launcher", f"--project={PROJECT}"):
        assert flag in call.split()
    # 初めてのデプロイでは、シークレットに関するフラグを付けない
    assert "secrets" not in call
    # .env に書かれたトークンを、コマンドの引数へ渡さない
    assert "dummy-bot-token-must-not-leak" not in "\n".join(sandbox.calls())

    # Slack を使っていたインスタンスを Slack なしでデプロイし直す場合は、シークレットの参照を外す
    assert sandbox.run("deploy_agent.sh").returncode == 0
    assert "--clear-secrets" in _deploy_call(sandbox).split()


def test_deploy_adds_slack_only_when_secrets_and_channel_are_ready(sandbox: Sandbox) -> None:
    """Slack は、シークレットがあり、.env に実際のチャンネル ID があるときだけ有効にすること。"""
    assert sandbox.run("setup.sh", stdin=f"{PROJECT}\n").returncode == 0

    # シークレットはあるが、チャンネル ID が .env.example の仮の値のまま
    placeholder = sandbox.run("deploy_agent.sh", slack_secrets="1")
    assert placeholder.returncode == 0, placeholder.stdout + placeholder.stderr
    assert "Slack        : 無効" in placeholder.stdout
    assert "--set-secrets" not in _deploy_call(sandbox)

    sandbox.set_env("SLACK_ALERT_CHANNEL_ID", "C0123ABCDEF")
    sandbox.set_env("SLACK_ALLOWED_APPROVER_IDS", "U0000000001,U0000000002")

    # チャンネル ID はあるが、シークレットが無い
    no_secrets = sandbox.run("deploy_agent.sh")
    assert no_secrets.returncode == 0, no_secrets.stdout + no_secrets.stderr
    assert "Slack        : 無効" in no_secrets.stdout

    both = sandbox.run("deploy_agent.sh", slack_secrets="1")
    assert both.returncode == 0, both.stdout + both.stderr
    assert "Slack        : 有効" in both.stdout
    assert "メール通知   : 有効" in both.stdout
    call = _deploy_call(sandbox)
    assert "--set-secrets=SLACK_BOT_TOKEN=sre-slack-bot-token:latest,SLACK_APP_TOKEN=sre-slack-app-token:latest" in call.split()
    assert "--clear-secrets" not in call
    # 承認者はカンマ区切りのまま 1 つの値として渡す
    assert "@SLACK_ALERT_CHANNEL_ID=C0123ABCDEF@SLACK_ALLOWED_APPROVER_IDS=U0000000001,U0000000002@" in call
    assert "@EMAIL_NOTIFY_INTEGRATION=sre-notify-email@" in call


def test_cleanup_removes_costly_resources_first_and_skips_missing_ones(sandbox: Sandbox) -> None:
    """課金額の大きいものから削除し、ロールを取り消してからサービスアカウントを消し、既に無いものは飛ばすこと。"""
    first = sandbox.run("cleanup.sh", stdin=f"{PROJECT}\n", existing="1")
    assert first.returncode == 0, first.stdout + first.stderr
    assert "後片付けが完了しました" in first.stdout

    calls = sandbox.calls()
    order = _positions(
        calls,
        "gcloud beta run instances delete",
        "gcloud container clusters delete",
        "gcloud compute routers nats delete",
        "gcloud compute routers delete",
        "gcloud compute firewall-rules delete gke-leftover-rule",
        "gcloud compute networks subnets delete",
        "gcloud compute networks delete",
        "setup_slo_monitoring.py --delete",
        "setup_email_notification.py --delete",
        "gcloud logging sinks delete",
        "gcloud pubsub subscriptions delete",
        "gcloud pubsub topics delete",
        "bq rm -r -f -d",
        "bq rm --connection",
        "gcloud artifacts repositories delete",
        "gcloud iam service-accounts delete",
    )
    assert order == sorted(order)
    assert _count(calls, "gcloud pubsub subscriptions delete") == 2
    assert _count(calls, "gcloud pubsub topics delete") == 2
    assert _count(calls, "gcloud iam service-accounts delete") == 3
    # サービスアカウント 3 つの 18 個と、BigQuery の接続用サービスアカウントの 1 個
    assert _count(calls, "gcloud projects remove-iam-policy-binding") == 19
    for account in ("sre-autonomous-agent-sa", "sre-gke-node-sa", "sre-cloudbuild-sa"):
        revoked = [index for index, call in enumerate(calls) if "remove-iam-policy-binding" in call and f":{account}@" in call]
        (deleted,) = _positions(calls, f"gcloud iam service-accounts delete {account}@")
        assert revoked and max(revoked) < deleted
    # プロジェクト、API、Firestore、Slack のシークレットには触れない
    for fragment in ("projects delete", "services disable", "firestore databases delete", "secrets delete"):
        assert _count(calls, fragment) == 0, fragment

    checkpoint = len(calls)
    second = sandbox.run("cleanup.sh", stdin=f"{PROJECT}\n", existing="1")
    assert second.returncode == 0, second.stdout + second.stderr
    repeated = sandbox.calls()[checkpoint:]
    assert [call for call in repeated if call.startswith(("gcloud", "bq")) and _is_change(call)] == []

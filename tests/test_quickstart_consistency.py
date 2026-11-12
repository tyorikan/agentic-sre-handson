"""クイックスタートのスクリプトが、フル版の手順と同じ内容を保っていることを照合するテスト。

quickstart/ のスクリプトは、フル版のドキュメントに載っているコマンドを 1 回の実行にまとめたものです。
片方だけを変更すると、クイックスタートとフル版で構築される環境が食い違います。
ここでは、ドキュメントのコマンドとスクリプトのコマンドを機械的に読み取って照合します。

クラウドへは接続しません。スクリプトの分岐（作成済みなら飛ばす、など）は tests/test_quickstart_scripts.py で確かめます。
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS = REPO_ROOT / "docs"
QUICKSTART = REPO_ROOT / "quickstart"

DOC_PREREQUISITES = "00-overview-and-architecture.md"
DOC_TARGET_APP = "01-target-gke-and-alerting.md"
DOC_KNOWLEDGE_BASE = "02-bigquery-vector-kb.md"
DOC_NOTIFICATION = "04-auto-remediation-and-slack-hitl.md"
DOC_DEPLOY = "05-deploy-cloud-run-instance-and-drills.md"
DOC_CLEANUP = "06-optional-hardening-and-cleanup.md"

RUNNABLE_SCRIPTS = ("setup.sh", "deploy_agent.sh", "cleanup.sh")
SOURCED_SCRIPTS = ("env.sh", "lib.sh")

# 通知と承認の経路を決める環境変数。クイックスタートでは、使える経路を調べた結果から値を決める
ROUTE_ENV_VARS = {
    "SLACK_ALERT_CHANNEL_ID",
    "SLACK_ALLOWED_APPROVER_IDS",
    "EMAIL_NOTIFY_INTEGRATION",
    "APPROVAL_PUBSUB_SUBSCRIPTION",
}

_SHELL_WORDS = {"then", "||", "&&"}


def _doc_commands(name: str) -> str:
    """ドキュメントから、bash のコードブロックだけを取り出してつなげる。"""
    text = (DOCS / name).read_text(encoding="utf-8")
    return "\n".join(re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL))


def _script(name: str) -> str:
    """スクリプトの本文を、コメントだけの行を除いて返す。"""
    lines = (QUICKSTART / name).read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def _bash_array(text: str, name: str) -> list[str]:
    """NAME=( ... ) の形で定義された配列の要素を返す。"""
    found = re.search(rf"^{name}=\(\n(.*?)^\)", text, flags=re.DOTALL | re.MULTILINE)
    assert found, f"配列 {name} が見つかりません"
    return found.group(1).split()


def _fixed_names() -> dict[str, str]:
    """env.sh が決めているリソース名と既定値を、変数名から引けるようにする。"""
    text = (QUICKSTART / "env.sh").read_text(encoding="utf-8")
    names = dict(re.findall(r'^([A-Z_]+)="([A-Za-z0-9_.-]+)"$', text, flags=re.MULTILINE))
    names.update(re.findall(r'^: "\$\{([A-Z_]+):=([A-Za-z0-9_.-]+)\}"$', text, flags=re.MULTILINE))
    return names


def _resolve_names(text: str) -> str:
    """${VPC_NAME} や ${BQ_CONNECTION_ID:-sre_genai_conn} を、env.sh が決めている値へ置き換える。

    ドキュメントは名前を直接書き、スクリプトは変数で書いている箇所があるため、比べる前に表記をそろえる。
    """
    names = _fixed_names()
    return re.sub(
        r"\$\{([A-Z_]+)(?::-[^}]*)?\}",
        lambda matched: names.get(matched.group(1), matched.group(0)),
        text,
    )


def _command_arguments(text: str, command: str, occurrence: int = 0) -> list[str]:
    """text の中で command から始まる occurrence 番目のコマンドを、行の継続をつないで引数の一覧にする。"""
    starts = [matched.start() for matched in re.finditer(re.escape(command), text)]
    assert len(starts) > occurrence, f"「{command}」が {occurrence + 1} 個以上見つかりません"
    joined: list[str] = []
    for line in text[starts[occurrence] :].splitlines():
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            joined.append(stripped[:-1])
            continue
        joined.append(stripped)
        break
    tokens = shlex.split(_resolve_names(" ".join(joined)))[len(command.split()) :]
    return [token.rstrip(";") for token in tokens if token not in _SHELL_WORDS]


# (コマンド, ドキュメント, ドキュメント内の何番目か, スクリプト内の何番目か, スクリプトだけに付けてよい引数)
SETUP_COMMANDS = [
    ("gcloud firestore databases create", DOC_PREREQUISITES, 0, 0, set()),
    ("gcloud artifacts repositories create", DOC_TARGET_APP, 0, 0, set()),
    ("gcloud compute networks create", DOC_TARGET_APP, 0, 0, set()),
    ("gcloud compute networks subnets create", DOC_TARGET_APP, 0, 0, set()),
    ("gcloud compute routers create", DOC_TARGET_APP, 0, 0, set()),
    ("gcloud compute routers nats create", DOC_TARGET_APP, 0, 0, set()),
    # スクリプトは、クラスタの作成を待つ間に他の段階を進めるため --async を付ける
    ("gcloud container clusters create-auto", DOC_TARGET_APP, 0, 0, {"--async"}),
    ("gcloud container clusters get-credentials", DOC_TARGET_APP, 0, 0, set()),
    ("gcloud builds submit", DOC_TARGET_APP, 0, 0, set()),
    ("gcloud builds submit", DOC_DEPLOY, 0, 1, set()),
    ("gcloud pubsub topics create", DOC_TARGET_APP, 0, 0, set()),
    ("gcloud pubsub topics create", DOC_NOTIFICATION, 0, 1, set()),
    ("gcloud pubsub subscriptions create", DOC_TARGET_APP, 0, 0, set()),
    ("gcloud pubsub subscriptions create", DOC_NOTIFICATION, 0, 1, set()),
    ("gcloud logging sinks create", DOC_TARGET_APP, 0, 0, set()),
    ("bq mk --connection", DOC_KNOWLEDGE_BASE, 0, 0, set()),
    ("bq load", DOC_KNOWLEDGE_BASE, 0, 0, set()),
]


@pytest.mark.parametrize(
    ("command", "doc", "doc_occurrence", "script_occurrence", "script_only"),
    SETUP_COMMANDS,
    ids=[f"{command} ({doc.split('-')[0]} 章)" for command, doc, _, _, _ in SETUP_COMMANDS],
)
def test_setup_runs_the_same_commands_as_the_full_course(
    command: str, doc: str, doc_occurrence: int, script_occurrence: int, script_only: set[str]
) -> None:
    """setup.sh が実行する作成コマンドの引数が、フル版のドキュメントのコマンドと同じであること。"""
    documented = set(_command_arguments(_doc_commands(doc), command, doc_occurrence))
    scripted = set(_command_arguments(_script("setup.sh"), command, script_occurrence))

    assert scripted - script_only == documented


def test_setup_enables_the_same_apis_as_the_full_course() -> None:
    """有効化する API が、第 0 章と第 4 章 5.2 で有効化するものと同じであること。"""
    documented = [
        token
        for doc in (DOC_PREREQUISITES, DOC_NOTIFICATION)
        for token in _command_arguments(_doc_commands(doc), "gcloud services enable")
        if token.endswith(".googleapis.com")
    ]
    scripted = _bash_array(_script("lib.sh"), "REQUIRED_APIS")

    assert sorted(scripted) == sorted(documented)
    assert len(scripted) == len(set(scripted))
    assert '"${REQUIRED_APIS[@]}"' in _script("setup.sh")


def test_setup_grants_the_same_roles_as_the_full_course() -> None:
    """3 つのサービスアカウントへ付与するロールが、第 0 章と第 4 章 5.2 で付与するものと同じであること。"""
    prerequisites = _doc_commands(DOC_PREREQUISITES)
    documented = {
        variable: re.findall(r"roles/[A-Za-z.]+", roles)
        for variable, roles in re.findall(r'^grant_roles "\$\{(\w+)\}" \\\n((?:\s+roles/\S+(?: \\)?\n?)+)', prerequisites, flags=re.MULTILINE)
    }
    assert set(documented) == {"SRE_BUILD_SA_EMAIL", "GKE_NODE_SA_EMAIL", "SRE_AGENT_SA_EMAIL"}

    # 第 4 章 5.2 では、通知メールを送る権限をエージェントのサービスアカウントへ追加する
    notification = _command_arguments(_doc_commands(DOC_NOTIFICATION), "gcloud projects add-iam-policy-binding")
    assert "--member=serviceAccount:sre-autonomous-agent-sa@${GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com" in notification
    documented["SRE_AGENT_SA_EMAIL"] += [token.split("=", 1)[1] for token in notification if token.startswith("--role=")]

    library = _script("lib.sh")
    scripted = {
        "SRE_BUILD_SA_EMAIL": _bash_array(library, "BUILD_SA_ROLES"),
        "GKE_NODE_SA_EMAIL": _bash_array(library, "NODE_SA_ROLES"),
        "SRE_AGENT_SA_EMAIL": _bash_array(library, "AGENT_SA_ROLES"),
    }
    assert {variable: sorted(roles) for variable, roles in scripted.items()} == {
        variable: sorted(roles) for variable, roles in documented.items()
    }

    # setup.sh は、配列で定義したロールを対応するサービスアカウントへ付与する
    setup = _script("setup.sh")
    for variable, array in (
        ("SRE_BUILD_SA_EMAIL", "BUILD_SA_ROLES"),
        ("GKE_NODE_SA_EMAIL", "NODE_SA_ROLES"),
        ("SRE_AGENT_SA_EMAIL", "AGENT_SA_ROLES"),
    ):
        assert f'qs_grant_roles "serviceAccount:${{{variable}}}" "${{{array}[@]}}"' in setup


def test_setup_uses_the_same_manifests_and_sql_as_the_full_course() -> None:
    """適用するマニフェストと SQL、書き換える目印が、第 1 章・第 2 章と同じであること。"""
    documented = _doc_commands(DOC_TARGET_APP) + _doc_commands(DOC_KNOWLEDGE_BASE)
    scripted = _script("setup.sh")
    file_pattern = r"\./(?:target-app/k8s|bigquery)/[\w./-]+"
    placeholder_pattern = r"s\|(\w+)\|"

    files = set(re.findall(file_pattern, documented))
    assert files == set(re.findall(file_pattern, scripted))
    assert len(files) == 6
    assert all((REPO_ROOT / path).is_file() for path in files)
    assert set(re.findall(placeholder_pattern, scripted)) == set(re.findall(placeholder_pattern, documented))


def _documented_agent_env() -> dict[str, str]:
    arguments = _command_arguments(_doc_commands(DOC_DEPLOY), "gcloud beta run instances deploy")
    value = next(token for token in arguments if token.startswith("--set-env-vars="))
    assert value.startswith("--set-env-vars=^@^")
    return dict(item.split("=", 1) for item in value.removeprefix("--set-env-vars=^@^").split("@"))


def _scripted_agent_env() -> dict[str, str]:
    found = re.search(r"local env_vars=\(\n(.*?)\n\s*\)", _script("deploy_agent.sh"), flags=re.DOTALL)
    assert found, "deploy_agent.sh に env_vars の定義が見つかりません"
    items = [line.strip() for line in _resolve_names(found.group(1)).splitlines()]
    assert all(item.startswith('"') and item.endswith('"') for item in items)
    return dict(item[1:-1].split("=", 1) for item in items)


def test_deploy_passes_the_same_settings_to_the_agent_as_the_full_course() -> None:
    """エージェントへ渡す環境変数が、第 5 章 Step 2.2 のコマンドと同じであること。"""
    documented = _documented_agent_env()
    scripted = _scripted_agent_env()

    assert list(scripted) == list(documented)
    assert ROUTE_ENV_VARS <= set(documented)
    for name in set(documented) - ROUTE_ENV_VARS:
        assert scripted[name] == documented[name], name
    # 訓練を続けて行うための設定は、フル版と同じく有効にする
    assert scripted["ALLOW_DRILL_COOLDOWN_RESET"] == "true"
    # 秘密の値は環境変数として渡さない（Slack のトークンは Secret Manager から渡す）
    assert not any("TOKEN" in name or "SECRET" in name for name in scripted)


def test_deploy_keeps_the_isolation_settings_of_the_full_course() -> None:
    """着信 URL を持たない設定や隔離サンドボックスなど、デプロイのフラグが第 5 章 Step 2.2 と同じであること。"""

    def _flags(text: str) -> set[str]:
        arguments = _command_arguments(text, "gcloud beta run instances deploy")
        return {token for token in arguments if token.startswith("--") and not token.startswith("--set-env-vars=")}

    documented = _flags(_doc_commands(DOC_DEPLOY))
    scripted = _flags(_script("deploy_agent.sh"))

    assert scripted == documented
    assert {"--ingress=internal", "--no-default-url", "--sandbox-launcher"} <= scripted

    # Slack を使う場合に Secret Manager から渡すトークンも、フル版と同じ
    secret_flag = '"--set-secrets=SLACK_BOT_TOKEN=sre-slack-bot-token:latest,SLACK_APP_TOKEN=sre-slack-app-token:latest"'
    assert secret_flag in _doc_commands(DOC_DEPLOY)
    assert secret_flag in _script("deploy_agent.sh")


def _removal_kinds(text: str) -> set[str]:
    """削除コマンドの種類（例: gcloud container clusters delete）を取り出す。"""
    kinds = set(re.findall(r"gcloud (?:[a-z-]+ )+delete\b", text))
    kinds.update(re.findall(r"bq rm (?:-r -f -d|--connection)", text))
    kinds.update(f"{script} --delete" for script in re.findall(r"(\w+\.py) --delete", text))
    return kinds


def test_cleanup_removes_everything_the_full_course_cleanup_removes() -> None:
    """cleanup.sh が、第 6 章の削除手順と同じ種類のリソースを削除すること。"""
    documented = _removal_kinds(_doc_commands(DOC_CLEANUP))
    scripted = _removal_kinds(_script("cleanup.sh"))

    assert len(documented) == 16
    # Slack のシークレットはクイックスタートでは作らないため、削除もしない
    assert documented - scripted == {"gcloud secrets delete"}
    # 第 6 章に無いのは、GKE が VPC に残すことがあるファイアウォール ルールの削除だけ
    assert scripted - documented == {"gcloud compute firewall-rules delete"}


def test_cleanup_removes_everything_setup_and_deploy_create() -> None:
    """setup.sh と deploy_agent.sh が作成するものを、cleanup.sh がすべて削除すること。"""
    created = _script("setup.sh") + _script("deploy_agent.sh")
    expected = {
        re.sub(r"(create-auto|create|deploy)$", "delete", kind)
        for kind in re.findall(r"gcloud (?:[a-z-]+ )+(?:create-auto|create|deploy)\b", created)
    }
    if "bq mk --connection" in created:
        expected.add("bq rm --connection")
    if "bigquery/01_schema.sql" in created:
        expected.add("bq rm -r -f -d")
    expected.update(f"{script} --delete" for script in re.findall(r"scripts/(setup_\w+\.py)", created))

    scripted = _removal_kinds(_script("cleanup.sh"))

    assert len(expected) == 16
    # Firestore のデータベースは、削除すると同じ名前では約 5 分間作り直せないため残す
    assert expected - scripted == {"gcloud firestore databases delete"}
    # プロジェクトと API は削除しない
    cleanup = _script("cleanup.sh")
    assert "projects delete" not in cleanup
    assert "services disable" not in cleanup


def test_quickstart_requirements_follow_pyproject() -> None:
    """クイックスタート用の最小構成のパッケージが、pyproject.toml と同じバージョン指定であること。"""
    lines = (QUICKSTART / "requirements.txt").read_text(encoding="utf-8").splitlines()
    requirements = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert requirements, "requirements.txt にパッケージがありません"
    assert set(requirements) <= set(project["dependencies"])


def test_quickstart_scripts_are_valid_and_stop_on_errors() -> None:
    """スクリプトに構文エラーがなく、実行するスクリプトは実行権限とエラー時の停止設定を持つこと。"""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash が必要です")

    for name in (*RUNNABLE_SCRIPTS, *SOURCED_SCRIPTS):
        checked = subprocess.run([bash, "-n", str(QUICKSTART / name)], capture_output=True, text=True, check=False)
        assert checked.returncode == 0, f"{name}: {checked.stderr}"

    for name in RUNNABLE_SCRIPTS:
        text = (QUICKSTART / name).read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash\n"), name
        assert os.access(QUICKSTART / name, os.X_OK), f"{name} に実行権限がありません"
        assert "\nset -Eeuo pipefail\n" in text, name
        assert "\ntrap qs_on_error ERR\n" in text, name


def test_mail_count_in_setup_matches_the_documented_drills() -> None:
    """メールの送信上限の確認に使う通数が、手順書に書いたクイックスタート 1 周の通数と同じであること。"""
    count = re.search(r"^QUICKSTART_EMAIL_COUNT=(\d+)$", _script("lib.sh"), flags=re.MULTILINE)
    assert count, "QUICKSTART_EMAIL_COUNT が見つかりません"
    guide = (DOCS / "quickstart.md").read_text(encoding="utf-8")

    assert f"合計 {count.group(1)} 通" in guide
    assert '--min-daily-limit "${QUICKSTART_EMAIL_COUNT}"' in _script("setup.sh")


def test_full_course_cleanup_revokes_the_same_roles_before_deleting_service_accounts() -> None:
    """フル版 第 6 章の削除手順も、サービスアカウントを削除する前に付与済みの全ロールを取り消すこと。"""
    cleanup_doc = _doc_commands(DOC_CLEANUP)
    revoked = {
        sa_name: re.findall(r"roles/[A-Za-z.]+", roles)
        for sa_name, roles in re.findall(
            r'^revoke_roles "(sre-[a-z-]+)@\$\{GOOGLE_CLOUD_PROJECT\}\.iam\.gserviceaccount\.com" \\\n((?:\s+roles/\S+(?: \\)?\n?)+)',
            cleanup_doc,
            flags=re.MULTILINE,
        )
    }
    library = _script("lib.sh")
    expected = {
        "sre-cloudbuild-sa": _bash_array(library, "BUILD_SA_ROLES"),
        "sre-gke-node-sa": _bash_array(library, "NODE_SA_ROLES"),
        "sre-autonomous-agent-sa": _bash_array(library, "AGENT_SA_ROLES"),
    }
    assert {sa: sorted(roles) for sa, roles in revoked.items()} == {
        sa: sorted(roles) for sa, roles in expected.items()
    }
    # BigQuery 接続用サービスアカウントへ付与した roles/aiplatform.user の取り消しも記載されていること
    assert '--member="serviceAccount:${BQ_CONN_SA}"' in cleanup_doc
    assert cleanup_doc.index("revoke_roles") < cleanup_doc.index("gcloud iam service-accounts delete")


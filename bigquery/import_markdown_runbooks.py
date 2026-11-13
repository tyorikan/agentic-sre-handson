#!/usr/bin/env python3
"""既存の障害対応手順書（Markdown / テキスト）を、知識ベース投入用の JSONL へ変換する CLI。

手元にある手順書や障害報告書を 1 ファイル 1 件として読み込み、`incident_runbooks_raw` と同じ 15 項目の
JSONL に変換します。変換した JSONL は、内容を人が確認してから `03_seed_production_starter.sql` で投入します。

安全のための固定ルール（LLM の出力や手順書の内容にかかわらず、スクリプトが決定論的に適用します）:
  - `recommended_action` は常に `REQUIRE_APPROVAL`。取り込んだ手順は、このエージェントの環境でまだ検証されていないためです。
  - `remediation_type` が `ROLLBACK_DEPLOYMENT` / `RESTART_DEPLOYMENT` 以外なら `MANUAL_INTERVENTION`。
  - `historical_mttr_minutes` は常に 0（実績値なし）。復旧時間を LLM に推定させません。

変換方法は 2 つあります。
  - 既定: Gemini の構造化出力で項目を抽出します（Gemini を呼び出すため、少額の利用料金がかかります）。
  - `--offline`: API を呼ばず、見出しの語から項目を拾います。精度は低く、操作種別は常に `MANUAL_INTERVENTION` になります。

使い方:
  # 変換結果の一覧だけを表示する（ファイルは書き出さない）
  python3 ./bigquery/import_markdown_runbooks.py --input ./my_runbooks --dry-run

  # JSONL へ書き出す
  python3 ./bigquery/import_markdown_runbooks.py --input ./my_runbooks --output ./bigquery/custom_runbooks.jsonl

  # API を呼ばずに変換する
  python3 ./bigquery/import_markdown_runbooks.py --input ./my_runbooks --output /tmp/out.jsonl --offline
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# LLM へ渡す本文の上限（文字数）。これより長い手順書は先頭だけを使う
MAX_SOURCE_CHARS = 12000

# 承認後にエージェントが実行できる操作種別。これ以外は人が手順書に沿って作業する
EXECUTABLE_REMEDIATION_TYPES = ("ROLLBACK_DEPLOYMENT", "RESTART_DEPLOYMENT")

NOT_DOCUMENTED = "手順書に記載なし。投入前に追記すること。"


class ExtractedRunbookRecord(BaseModel):
    """手順書 1 件から抽出する項目。判断区分と復旧時間は含めない（スクリプトが固定値を入れる）。"""

    category: str = Field(description="障害の分類。例: データベース・キャッシュ / ネットワーク・ロードバランサ・DNS")
    title: str = Field(description="症状と原因が分かる簡潔な件名")
    service_pattern: str = Field(description="対象の構成の種類。例: GKE Deployment (HTTP API) + Cloud SQL for PostgreSQL")
    symptom_signature: str = Field(description="ログやイベントに出る代表的な文言（例外クラス名、HTTP ステータス、Kubernetes の Reason）")
    metrics_pattern: str = Field(description="発生時の指標の変わり方")
    recent_change_hint: str = Field(description="直前のデプロイや設定変更との関係")
    root_cause: str = Field(description="根本原因")
    remediation_type: str = Field(
        description=(
            "手順書の主な対処が Kubernetes Deployment のロールバックなら ROLLBACK_DEPLOYMENT、"
            "Deployment の再起動なら RESTART_DEPLOYMENT、それ以外はすべて MANUAL_INTERVENTION"
        )
    )
    remediation_steps: str = Field(description="対処の手順")
    anti_patterns: str = Field(description="やってはいけない対応と、その理由")
    verification_check: str = Field(description="回復したと判断する確認方法")


def collect_source_files(target: Path) -> list[Path]:
    """単一ファイル、またはディレクトリ配下の `.md` / `.markdown` / `.txt` を名前順に返す。"""
    if target.is_file():
        return [target]
    if not target.is_dir():
        raise FileNotFoundError(f"入力パスが見つかりません: {target}")
    return sorted(
        p
        for p in target.rglob("*")
        if p.is_file() and p.suffix.lower() in (".md", ".markdown", ".txt") and not p.name.startswith(".")
    )


def build_runbook_id(prefix: str, relative_path: str) -> str:
    """ファイルの相対パスから ID を作る。本文を直しても同じファイルなら同じ ID になる。"""
    digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:8].upper()
    return f"{prefix}-{digest}"


def to_safe_record(raw: ExtractedRunbookRecord, runbook_id: str) -> dict[str, Any]:
    """抽出結果に固定ルールを適用し、JSONL 1 行分（15 項目）の辞書にする。"""
    remediation_type = raw.remediation_type.strip().upper()
    if remediation_type not in EXECUTABLE_REMEDIATION_TYPES:
        remediation_type = "MANUAL_INTERVENTION"

    return {
        "runbook_id": runbook_id,
        "category": raw.category.strip() or "取り込んだ手順書",
        "title": raw.title.strip(),
        "service_pattern": raw.service_pattern.strip() or NOT_DOCUMENTED,
        "symptom_signature": raw.symptom_signature.strip() or NOT_DOCUMENTED,
        "metrics_pattern": raw.metrics_pattern.strip() or NOT_DOCUMENTED,
        "recent_change_hint": raw.recent_change_hint.strip() or NOT_DOCUMENTED,
        "root_cause": raw.root_cause.strip() or NOT_DOCUMENTED,
        "recommended_action": "REQUIRE_APPROVAL",
        "remediation_type": remediation_type,
        "remediation_steps": raw.remediation_steps.strip() or NOT_DOCUMENTED,
        "anti_patterns": raw.anti_patterns.strip() or NOT_DOCUMENTED,
        "verification_check": raw.verification_check.strip() or NOT_DOCUMENTED,
        "historical_mttr_minutes": 0,
        "verified_age_days": 0,
    }


def parse_markdown_offline(md_text: str, fallback_title: str) -> ExtractedRunbookRecord:
    """API を呼ばず、見出しの語を手がかりに項目を拾う。

    見出しの付け方に強く依存するため、拾えなかった項目には「手順書に記載なし」と入れます。
    操作種別は本文から推測せず、常に `MANUAL_INTERVENTION` にします
    （「ロールバックしてはいけない」のような否定の文を、語の一致だけでは区別できないためです）。
    """
    lines = [ln.strip() for ln in md_text.splitlines()]
    title = next((ln[2:].strip() for ln in lines if ln.startswith("# ")), fallback_title)

    sections: dict[str, list[str]] = {"_lead": []}
    current = "_lead"
    for ln in lines:
        heading = re.match(r"^#{2,4}\s+(.+)$", ln)
        if heading:
            current = heading.group(1).strip().lower()
            sections.setdefault(current, [])
        elif ln and not ln.startswith("# "):
            sections[current].append(ln.lstrip("-* "))

    def pick(*keywords: str) -> str:
        for name, body in sections.items():
            if body and any(k in name for k in keywords):
                return " / ".join(body[:3])[:400]
        return ""

    lead = " / ".join(sections["_lead"][:3])[:300]
    return ExtractedRunbookRecord(
        category="取り込んだ手順書",
        title=title,
        service_pattern="",
        symptom_signature=pick("symptom", "error", "兆候", "症状", "エラー", "検知", "アラート") or lead or title,
        metrics_pattern=pick("metric", "slo", "メトリクス", "指標"),
        recent_change_hint=pick("change", "deploy", "変更", "デプロイ"),
        root_cause=pick("cause", "原因", "背景"),
        remediation_type="MANUAL_INTERVENTION",
        remediation_steps=pick("remediat", "mitigat", "resolution", "fix", "復旧", "対処", "対応", "手順"),
        anti_patterns=pick("anti", "caution", "warning", "注意", "禁止", "やってはいけない"),
        verification_check=pick("verif", "confirm", "確認", "検証", "回復"),
    )


def extract_with_gemini(md_text: str, fallback_title: str, model_name: str) -> ExtractedRunbookRecord:
    """Gemini の構造化出力で、自由形式の手順書から項目を抽出する。"""
    from google import genai
    from google.genai import types

    prompt = (
        "次の障害対応手順書を読み、障害対応エージェントの知識ベースに入れる項目を抽出してください。\n"
        "- 本文に書かれていないサービス名、コマンド、数値を作らないこと。書かれていない項目は空文字にすること。\n"
        "- service_pattern には固有のサービス名ではなく、構成の種類を書くこと。\n"
        "- remediation_type は、主な対処が Kubernetes Deployment のロールバックまたは再起動だと本文に明記されている"
        "場合だけ ROLLBACK_DEPLOYMENT / RESTART_DEPLOYMENT とし、少しでも迷う場合は MANUAL_INTERVENTION とすること。\n\n"
        f"# 文書名: {fallback_title}\n\n"
        f"{md_text[:MAX_SOURCE_CHARS]}"
    )
    response = genai.Client().models.generate_content(
        model=model_name,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ExtractedRunbookRecord,
            thinking_config=types.ThinkingConfig(thinking_level="MEDIUM"),
        ),
    )
    if not response.text:
        raise RuntimeError("モデルから応答本文が返りませんでした。")
    return ExtractedRunbookRecord.model_validate_json(response.text)


def convert_files_to_records(
    files: list[Path],
    *,
    base_dir: Path,
    prefix: str = "CUST",
    offline: bool = False,
    model_name: str = "",
) -> tuple[list[dict[str, Any]], list[str]]:
    """ファイル群を JSONL 用の辞書へ変換する。戻り値は（変換できた行, 変換できなかったファイルと理由）。"""
    records: list[dict[str, Any]] = []
    failures: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
        if not text:
            failures.append(f"{path}: 本文が空です")
            continue
        relative = path.relative_to(base_dir).as_posix() if path.is_relative_to(base_dir) else path.name
        fallback_title = path.stem.replace("_", " ").replace("-", " ")
        try:
            extracted = (
                parse_markdown_offline(text, fallback_title)
                if offline
                else extract_with_gemini(text, fallback_title, model_name)
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{path}: {type(exc).__name__}: {exc}")
            continue
        records.append(to_safe_record(extracted, build_runbook_id(prefix, relative)))
    return records, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="既存の障害対応手順書を、知識ベース投入用の JSONL へ変換します。")
    parser.add_argument("--input", required=True, type=Path, help="変換元のファイルまたはディレクトリ（.md / .markdown / .txt）")
    parser.add_argument("--output", type=Path, default=None, help="出力先の JSONL。省略すると一覧表示だけを行う")
    parser.add_argument("--prefix", default="CUST", help="runbook_id の接頭辞（既定: CUST）")
    parser.add_argument(
        "--model",
        default=os.getenv("SRE_TRIAGE_MODEL", "gemini-4-hydrogen"),
        help="項目の抽出に使う Gemini モデル（既定は環境変数 SRE_TRIAGE_MODEL）",
    )
    parser.add_argument("--offline", action="store_true", help="API を呼ばず、見出しの語から項目を拾う")
    parser.add_argument("--dry-run", action="store_true", help="ファイルを書き出さず、変換結果の一覧だけを表示する")
    args = parser.parse_args(argv)

    if not args.offline:
        from dotenv import load_dotenv

        load_dotenv()

    files = collect_source_files(args.input)
    if not files:
        print(f"変換対象のファイルが見つかりません: {args.input}", file=sys.stderr)
        return 1

    base_dir = args.input if args.input.is_dir() else args.input.parent
    records, failures = convert_files_to_records(
        files,
        base_dir=base_dir,
        prefix=args.prefix,
        offline=args.offline,
        model_name=args.model,
    )

    for rec in records:
        print(f"{rec['runbook_id']}  [{rec['recommended_action']} / {rec['remediation_type']}]  {rec['title']}")
    for failure in failures:
        print(f"変換できませんでした: {failure}", file=sys.stderr)

    if args.dry_run or args.output is None:
        print(f"一覧表示のみ: {len(records)} 件を変換、{len(failures)} 件は変換できませんでした（ファイルは書き出していません）")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
            encoding="utf-8",
        )
        print(f"書き出し完了: {len(records)} 件 -> {args.output}（{len(failures)} 件は変換できませんでした）")
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

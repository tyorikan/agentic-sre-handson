#!/usr/bin/env python3
"""訓練前後の Error Budget 消費量と自動復旧による温存効果を確認・比較する CLI ツール。

機能:
  1. Cloud Monitoring Service Monitoring API から現在の日次 99.0% / 月次 99.9% SLO の
     Error Budget 残量（%・残り分数）、SLI 達成率、直近 10 分の燃焼率（Burn Rate）を取得表示する。
  2. あわせて直近 N 分間の Cloud Logging 実注文ログ（正常・異常件数と異常発生分数）を集計し、
     メトリクス反映遅延中（直後 1〜2 分）でも即座に実測 SLI を確認できるようにする。
  3. `--save-baseline` で訓練前の状態を保存し、`--compare-baseline` で訓練後の実測消費差分および
     「従来の人間手動対応（既定 30 分）だった場合との比較表」を出力する。

使い方:
  # 現在値の確認と訓練前ベースライン保存
  python3 ./target-app/scripts/check_slo_budget.py --save-baseline /tmp/slo_before.json

  # 障害訓練完了後に実行し、訓練前後の Error Budget 消費差分と温存効果を表示
  python3 ./target-app/scripts/check_slo_budget.py --compare-baseline /tmp/slo_before.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.cloud import logging_v2

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "sre-agent"))

from agent_app.config import load_settings  # noqa: E402
from agent_app.slo_metrics import (  # noqa: E402
    CloudMonitoringSloReader,
    DAILY_99_TOTAL_BUDGET_MINUTES,
    MONTHLY_999_TOTAL_BUDGET_MINUTES,
    SloBudgetSnapshot,
    SloWindowStatus,
)


def collect_recent_order_log_stats(
    project_id: str,
    *,
    namespace: str = "sre-demo-app",
    service_name: str = "order-service",
    lookback_minutes: int = 15,
) -> dict[str, Any]:
    """直近 lookback_minutes 分間の POST /orders 構造化ログから実リクエスト数と異常分数を集計する。"""
    client = logging_v2.Client(project=project_id)
    since = (datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)).isoformat()
    log_filter = (
        'resource.type="k8s_container" '
        f'AND resource.labels.namespace_name="{namespace}" '
        f'AND resource.labels.container_name="{service_name}" '
        f'AND jsonPayload.service="{service_name}" '
        "AND (jsonPayload.order_id:* OR jsonPayload.error_code:*) "
        f'AND timestamp>="{since}"'
    )
    per_minute: dict[str, list[int]] = {}  # minute_key -> [good, total]
    good = 0
    bad = 0
    for entry in client.list_entries(filter_=log_filter, page_size=500, max_results=2000):
        ts: datetime = entry.timestamp or datetime.now(timezone.utc)
        mkey = ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        counts = per_minute.setdefault(mkey, [0, 0])
        counts[1] += 1
        sev = str(entry.severity or "INFO").upper()
        if sev == "INFO":
            good += 1
            counts[0] += 1
        else:
            bad += 1

    total = good + bad
    good_minutes = sum(1 for g, t in per_minute.values() if t > 0 and (g / t) >= 0.95)
    bad_minutes = sum(1 for g, t in per_minute.values() if t > 0 and (g / t) < 0.95)
    return {
        "lookback_minutes": lookback_minutes,
        "total_requests": total,
        "good_requests": good,
        "bad_requests": bad,
        "active_minutes": len(per_minute),
        "good_minutes": good_minutes,
        "bad_minutes": bad_minutes,
    }


def _fmt_pct(val: float | None, digits: int = 2) -> str:
    return f"{val:.{digits}f}%" if val is not None else "集計待機中"


def _fmt_min(val: float | None) -> str:
    return f"{val:.2f} 分" if val is not None else "集計待機中"


def _fmt_burn(val: float | None) -> str:
    return f"{val:.2f}x" if val is not None else "0.00x"


def format_comparison_report(
    before: SloBudgetSnapshot | None,
    after: SloBudgetSnapshot | None,
    *,
    manual_mttr_minutes: float = 30.0,
    observed_bad_minutes_delta: int | None = None,
) -> str:
    """訓練前後の SLO スナップショットから消費差分と自動復旧の温存効果を整形する。"""
    lines: list[str] = []
    lines.append("\n=== 訓練前後 Error Budget 消費比較 & 自動復旧の効果 ===")

    def _delta_line(label: str, b_slo: SloWindowStatus | None, a_slo: SloWindowStatus | None, total_min: float) -> None:
        if b_slo and a_slo and b_slo.budget_remaining_pct is not None and a_slo.budget_remaining_pct is not None:
            before_pct = b_slo.budget_remaining_pct
            after_pct = a_slo.budget_remaining_pct
            consumed_pct = max(0.0, round(before_pct - after_pct, 2))
            consumed_min = round((consumed_pct / 100.0) * total_min, 2)
            manual_pct = round((manual_mttr_minutes / total_min) * 100.0, 1)
            saved_min = max(0.0, round(manual_mttr_minutes - consumed_min, 2))
            lines.append(
                f"* {label}:\n"
                f"  - 訓練前残量: {before_pct:.2f}%  ->  訓練後残量: {after_pct:.2f}% "
                f"(実測消費: -{consumed_pct:.2f}% / 約 {consumed_min:.2f} 分)\n"
                f"  - 仮に手動対応 {manual_mttr_minutes:.0f} 分かかった場合（仮定ベースライン）: -{manual_pct:.1f}% 消費 "
                f"-> 自動化により約 {saved_min:.2f} 分のバジェットを温存 [推定値]"
            )
        else:
            lines.append(f"* {label}: Cloud Monitoring 時系列アライメント反映待ち（約 60〜90 秒後に再実行すると実測差分が表示されます）")

    _delta_line(
        "日次 99.0% SLO [許容 14.4 分/日]",
        before.daily_slo if before else None,
        after.daily_slo if after else None,
        DAILY_99_TOTAL_BUDGET_MINUTES,
    )
    _delta_line(
        "月次 99.9% SLO [許容 43.2 分/月]",
        before.monthly_slo if before else None,
        after.monthly_slo if after else None,
        MONTHLY_999_TOTAL_BUDGET_MINUTES,
    )

    if observed_bad_minutes_delta is not None and observed_bad_minutes_delta >= 0:
        saved_vs_manual = max(0.0, manual_mttr_minutes - float(observed_bad_minutes_delta))
        lines.append(
            f"* 直近構造化ログ実測の異常分数増分: +{observed_bad_minutes_delta} 分 "
            f"(手動対応 {manual_mttr_minutes:.0f} 分基準と比べ 約 {saved_vs_manual:.1f} 分の障害時間を短縮)"
        )
    return "\n".join(lines)


def main() -> int:
    load_dotenv(REPO_ROOT / ".env")
    settings = load_settings()

    parser = argparse.ArgumentParser(description="Cloud Monitoring SLO & Error Budget 状態確認・前後比較ツール")
    parser.add_argument("--save-baseline", type=Path, default=None, help="現在のスナップショットを JSON 保存するパス")
    parser.add_argument("--compare-baseline", type=Path, default=None, help="保存済みベースライン JSON と比較するパス")
    parser.add_argument("--lookback-minutes", type=int, default=15, help="直近注文ログ集計の対象分数")
    parser.add_argument("--skip-log-stats", action="store_true", help="Cloud Logging 直近リクエスト集計を省略する")
    args = parser.parse_args()

    if not settings.project_id or settings.project_id == "your-sandbox-project-id":
        print("エラー: .env の GOOGLE_CLOUD_PROJECT を設定してください。", file=sys.stderr)
        return 1

    reader = CloudMonitoringSloReader(settings)
    snapshot = reader.read_current_snapshot()
    log_stats = (
        None
        if args.skip_log_stats
        else collect_recent_order_log_stats(settings.project_id, lookback_minutes=args.lookback_minutes)
    )

    print(f"=== Order Service SLO & Error Budget Status ({settings.project_id}) ===")
    if snapshot is None:
        print("Cloud Monitoring SLO 時系列データがまだ生成されていません（setup_slo_monitoring.py 実行後 2〜3 分お待ちください）。")
    else:
        for slo in (snapshot.daily_slo, snapshot.monthly_slo):
            if slo is None:
                continue
            print(f"\n[{slo.display_name}]")
            print(f"  SLO 達成率 (Compliance) : {_fmt_pct(slo.compliance_pct, 3)} (目標: {slo.goal * 100:.1f}%)")
            print(f"  Error Budget 残量       : {_fmt_pct(slo.budget_remaining_pct)} ({_fmt_min(slo.budget_remaining_minutes)} / 総枠 {slo.total_budget_minutes:.1f} 分)")
            print(f"  直近10分 Burn Rate      : {_fmt_burn(slo.burn_rate_10m)}")

    if log_stats is not None:
        req_sli = (
            (log_stats["good_requests"] / log_stats["total_requests"]) * 100.0
            if log_stats["total_requests"] > 0
            else 100.0
        )
        print(f"\n[直近 {log_stats['lookback_minutes']} 分間の実注文ログ集計 (POST /orders)]")
        print(
            f"  リクエスト内訳 : 合計 {log_stats['total_requests']} 件 "
            f"(正常 201: {log_stats['good_requests']} 件 / 異常 5xx: {log_stats['bad_requests']} 件 / 成功率: {req_sli:.2f}%)"
        )
        print(
            f"  1分窓カウント  : 計測 {log_stats['active_minutes']} 分 "
            f"(正常分 Good: {log_stats['good_minutes']} 分 / 異常分 Bad: {log_stats['bad_minutes']} 分)"
        )

    if args.save_baseline is not None:
        payload = {
            "snapshot": snapshot.model_dump() if snapshot else None,
            "log_stats": log_stats,
        }
        args.save_baseline.parent.mkdir(parents=True, exist_ok=True)
        args.save_baseline.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[SAVED] 訓練前ベースラインを保存しました: {args.save_baseline}")

    if args.compare_baseline is not None:
        if not args.compare_baseline.exists():
            print(f"エラー: ベースラインファイルが見つかりません: {args.compare_baseline}", file=sys.stderr)
            return 1
        raw_before = json.loads(args.compare_baseline.read_text(encoding="utf-8"))
        before_snap = (
            SloBudgetSnapshot.model_validate(raw_before["snapshot"])
            if raw_before.get("snapshot")
            else None
        )
        before_logs = raw_before.get("log_stats")
        bad_min_delta: int | None = None
        if before_logs and log_stats:
            bad_min_delta = max(0, int(log_stats["bad_minutes"]) - int(before_logs.get("bad_minutes", 0)))
        print(
            format_comparison_report(
                before_snap,
                snapshot,
                manual_mttr_minutes=settings.manual_baseline_mttr_minutes,
                observed_bad_minutes_delta=bad_min_delta,
            )
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

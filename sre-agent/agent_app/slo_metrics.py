"""Cloud Monitoring SLO 状態読取と Error Budget 温存効果（Saved Error Budget）の定量計算モジュール。

役割:
  1. 異常検知時刻（`AlertEvent.observed_at`）から回復検証完了時刻までの実測 MTTR（秒・分）を算出する。
  2. 人間オンコール対応時の標準復旧時間（既定 30.0 分）と比較し、早期の自動復旧または
     人の承認による復旧によって消費を回避できた Error Budget 分数・割合を算出する。
  3. Cloud Monitoring API v3 の SLO 時系列セレクタ（`select_slo_budget_fraction`,
     `select_slo_budget`, `select_slo_compliance`, `select_slo_burn_rate`）を呼び出し、
     日次 99.0% SLO（許容 14.4 分/日）および月次 99.9% SLO（許容 43.2 分/月）の実測残量を取得する。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

from google.api_core import exceptions as gcp_exceptions
from google.cloud import monitoring_v3
from pydantic import BaseModel, Field

from .config import AgentSettings
from .models import AlertEvent, DecisionType, RemediationOutcome

# カレンダー日次 (1,440分) の 99.0% 可用性 SLO における許容エラーバジェット = 14.4 分
DAILY_99_TOTAL_BUDGET_MINUTES: float = 1440.0 * (1.0 - 0.99)

# 30日換算 (43,200分) の 99.9% 可用性 SLO における許容エラーバジェット = 43.2 分
MONTHLY_999_TOTAL_BUDGET_MINUTES: float = 43200.0 * (1.0 - 0.999)


class SloWindowStatus(BaseModel):
    """単一 SLO（日次または月次）の特定時点における実測ステータスメトリクス。"""

    slo_id: str
    display_name: str
    goal: float
    total_budget_minutes: float
    budget_remaining_pct: float | None = Field(
        default=None,
        description="Error Budget 残量パーセント（例: 91.5 = 91.5% 残存、負値は SLO 違反超過）",
    )
    budget_remaining_minutes: float | None = Field(
        default=None,
        description="Error Budget 残り分数（select_slo_budget の実測値）",
    )
    compliance_pct: float | None = Field(
        default=None,
        description="評価期間開始からの SLI 達成率パーセント（例: 99.88）",
    )
    burn_rate_10m: float | None = Field(
        default=None,
        description="直近 10 分間の Error Budget 燃焼率倍率（1.0 が許容上限ペース）",
    )


class SloBudgetSnapshot(BaseModel):
    """訓練前後比較および完了通知に用いる 2 本の可用性 SLO スナップショット。"""

    captured_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    project_id: str
    service_id: str
    daily_slo: SloWindowStatus | None = None
    monthly_slo: SloWindowStatus | None = None


class ErrorBudgetImpact(BaseModel):
    """1 回のインシデント対応における MTTR と Error Budget 温存効果の算出結果。"""

    mttr_seconds: float | None = None
    estimated_manual_mttr_minutes: float | None = None
    saved_error_budget_minutes: float | None = None
    monthly_budget_consumed_pct: float | None = None
    monthly_budget_saved_pct: float | None = None
    daily_budget_remaining_pct: float | None = None
    monthly_budget_remaining_pct: float | None = None
    burn_rate_10m: float | None = None
    impact_summary: str = ""


class SloReader(Protocol):
    """本番 Cloud Monitoring API 読取器とテスト用スタブの共通インターフェース。"""

    def read_current_snapshot(self) -> SloBudgetSnapshot | None:
        """現在の日次・月次 SLO スナップショットを返す（未構成時は None）。"""
        ...


def parse_utc_timestamp(raw_iso: str) -> datetime:
    """ISO-8601 文字列（末尾 Z を含む）を UTC aware datetime へ安全に変換する。"""
    normalized = raw_iso.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def calculate_error_budget_impact(
    *,
    alert: AlertEvent,
    decision: DecisionType | str,
    outcome: RemediationOutcome,
    manual_baseline_mttr_minutes: float = 30.0,
    completed_at: datetime | None = None,
    slo_snapshot: SloBudgetSnapshot | None = None,
) -> ErrorBudgetImpact:
    """インシデント検知から処置完了までの経過時間と処置結果から Error Budget 効果を算出する。

    - 自動復旧または承認復旧を実行し事後検証に合格したケースのみ、実測 MTTR と Saved Error Budget を計上する。
    - 一過性瞬断の静観（OBSERVE）や手動調査への引継（MANUAL_HANDOFF_ACKNOWLEDGED）では、
      実態と異なる復旧時間を計上せず、回避した二次被害を明記する。
    """
    end_dt = completed_at or datetime.now(timezone.utc)
    try:
        start_dt = parse_utc_timestamp(alert.observed_at)
        elapsed_seconds = max(0.0, (end_dt - start_dt).total_seconds())
    except ValueError:
        elapsed_seconds = 0.0

    daily_rem = (
        slo_snapshot.daily_slo.budget_remaining_pct
        if slo_snapshot and slo_snapshot.daily_slo
        else None
    )
    monthly_rem = (
        slo_snapshot.monthly_slo.budget_remaining_pct
        if slo_snapshot and slo_snapshot.monthly_slo
        else None
    )
    burn_10m = (
        slo_snapshot.daily_slo.burn_rate_10m
        if slo_snapshot and slo_snapshot.daily_slo and slo_snapshot.daily_slo.burn_rate_10m is not None
        else (
            slo_snapshot.monthly_slo.burn_rate_10m
            if slo_snapshot and slo_snapshot.monthly_slo
            else None
        )
    )

    # 1. 型付き復旧操作を実行し、事後エラー率 < 1.0% への回復が実証されたケース
    if outcome.executed and outcome.verification_passed:
        mttr_sec = round(elapsed_seconds, 1)
        actual_min = elapsed_seconds / 60.0
        manual_min = round(float(manual_baseline_mttr_minutes), 1)
        saved_min = round(max(0.0, manual_min - actual_min), 2)
        consumed_pct = round((actual_min / MONTHLY_999_TOTAL_BUDGET_MINUTES) * 100.0, 1)
        saved_pct = round((saved_min / MONTHLY_999_TOTAL_BUDGET_MINUTES) * 100.0, 1)
        # 承認の経路（Slack のボタン・承認コマンド）によらず同じ表記にする
        mode_label = "自動復旧" if decision == "AUTO_REMEDIATE" else "人の承認による復旧"
        summary = (
            f"実測 MTTR `{mttr_sec:.0f}秒` (`{actual_min:.2f}分`) — "
            f"手動対応基準 `{manual_min:.0f}分` と比べ Error Budget 消費を **`{saved_min:.1f}分` 削減** "
            f"[月次 99.9% 許容枠 43.2分のうち消費を `{consumed_pct:.1f}%` に抑え、`{saved_pct:.1f}%` 相当を{mode_label}で温存]"
        )
        return ErrorBudgetImpact(
            mttr_seconds=mttr_sec,
            estimated_manual_mttr_minutes=manual_min,
            saved_error_budget_minutes=saved_min,
            monthly_budget_consumed_pct=consumed_pct,
            monthly_budget_saved_pct=saved_pct,
            daily_budget_remaining_pct=daily_rem,
            monthly_budget_remaining_pct=monthly_rem,
            burn_rate_10m=burn_10m,
            impact_summary=summary,
        )

    # 2. 一過性エラーの自然回復を確認して静観したケース
    if decision == "OBSERVE" or outcome.action_status == "OBSERVED":
        obs_sec = round(elapsed_seconds, 1)
        return ErrorBudgetImpact(
            mttr_seconds=obs_sec,
            estimated_manual_mttr_minutes=None,
            saved_error_budget_minutes=0.0,
            monthly_budget_consumed_pct=None,
            monthly_budget_saved_pct=None,
            daily_budget_remaining_pct=daily_rem,
            monthly_budget_remaining_pct=monthly_rem,
            burn_rate_10m=burn_10m,
            impact_summary=(
                f"自然減衰を `{obs_sec:.0f}秒` で確認し静観 — "
                "不要な Pod 再起動による再接続ストームと追加の Error Budget 流出を防止"
            ),
        )

    # 3. DB ロック等の高リスク事象を人間へ安全に引き継いだケース（障害自体は手動対処継続中）
    if outcome.action_status == "MANUAL_HANDOFF_ACKNOWLEDGED":
        triage_sec = round(elapsed_seconds, 1)
        return ErrorBudgetImpact(
            mttr_seconds=None,
            estimated_manual_mttr_minutes=None,
            saved_error_budget_minutes=None,
            monthly_budget_consumed_pct=None,
            monthly_budget_saved_pct=None,
            daily_budget_remaining_pct=daily_rem,
            monthly_budget_remaining_pct=monthly_rem,
            burn_rate_10m=burn_10m,
            impact_summary=(
                f"検知から `{triage_sec:.0f}秒` で原因相関と証跡提示を完了しオンコール担当者へ引継 — "
                "無断再起動による DB 全面停止とバジェット全損を回避"
            ),
        )

    # 4. 却下または事後回復検証 NG のケース
    return ErrorBudgetImpact(
        mttr_seconds=None,
        estimated_manual_mttr_minutes=None,
        saved_error_budget_minutes=None,
        monthly_budget_consumed_pct=None,
        monthly_budget_saved_pct=None,
        daily_budget_remaining_pct=daily_rem,
        monthly_budget_remaining_pct=monthly_rem,
        burn_rate_10m=burn_10m,
        impact_summary="自動変更は未完了のため手動トリアージで Error Budget 影響を継続監視してください。",
    )


class CloudMonitoringSloReader:
    """Cloud Monitoring API v3 の SLO 時系列セレクタから実測値を取得するリーダー。"""

    def __init__(
        self,
        settings: AgentSettings,
        metric_client: monitoring_v3.MetricServiceClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = metric_client or monitoring_v3.MetricServiceClient()

    @staticmethod
    def _extract_latest_point_value(point: monitoring_v3.Point) -> float | None:
        val = point.value
        pb = val._pb  # Protobuf oneof フィールド判定用
        kind = pb.WhichOneof("value")
        if kind == "double_value":
            return float(val.double_value)
        if kind == "int64_value":
            return float(val.int64_value)
        return None

    def _query_latest_selector_value(
        self,
        selector_filter: str,
        *,
        lookback_minutes: int = 30,
    ) -> float | None:
        now = datetime.now(timezone.utc)
        interval = monitoring_v3.TimeInterval(
            start_time=now - timedelta(minutes=lookback_minutes),
            end_time=now,
        )
        req = monitoring_v3.ListTimeSeriesRequest(
            name=f"projects/{self._settings.project_id}",
            filter=selector_filter,
            interval=interval,
            view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        )
        try:
            for ts in self._client.list_time_series(request=req):
                if ts.points:
                    # list_time_series の points は新しい順（先頭が最新）で返却される
                    return self._extract_latest_point_value(ts.points[0])
        except (gcp_exceptions.NotFound, gcp_exceptions.InvalidArgument, gcp_exceptions.PermissionDenied):
            return None
        except Exception:  # noqa: BLE001
            return None
        return None

    def read_single_slo(
        self,
        *,
        slo_id: str,
        display_name: str,
        goal: float,
        total_budget_minutes: float,
    ) -> SloWindowStatus | None:
        """指定された 1 本の SLO について最新の残量・達成率・10 分燃焼率を取得する。"""
        if not self._settings.slo_service_id or not slo_id:
            return None
        slo_path = (
            f"projects/{self._settings.project_id}/services/{self._settings.slo_service_id}"
            f"/serviceLevelObjectives/{slo_id}"
        )
        frac = self._query_latest_selector_value(f'select_slo_budget_fraction("{slo_path}")')
        rem_min = self._query_latest_selector_value(f'select_slo_budget("{slo_path}")')
        comp = self._query_latest_selector_value(f'select_slo_compliance("{slo_path}")')
        burn = self._query_latest_selector_value(f'select_slo_burn_rate("{slo_path}", "600s")')

        if frac is None and rem_min is None and comp is None and burn is None:
            return None

        return SloWindowStatus(
            slo_id=slo_id,
            display_name=display_name,
            goal=goal,
            total_budget_minutes=total_budget_minutes,
            budget_remaining_pct=round(frac * 100.0, 2) if frac is not None else None,
            budget_remaining_minutes=round(rem_min, 2) if rem_min is not None else None,
            compliance_pct=round(comp * 100.0, 3) if comp is not None else None,
            burn_rate_10m=round(burn, 2) if burn is not None else None,
        )

    def read_current_snapshot(self) -> SloBudgetSnapshot | None:
        """日次 99.0% SLO と月次 99.9% SLO の最新スナップショットを取得する。"""
        if not self._settings.slo_service_id:
            return None
        daily = self.read_single_slo(
            slo_id=self._settings.slo_daily_id,
            display_name="日次 99.0% 可用性 SLO (許容 14.4 分/日)",
            goal=0.99,
            total_budget_minutes=DAILY_99_TOTAL_BUDGET_MINUTES,
        )
        monthly = self.read_single_slo(
            slo_id=self._settings.slo_monthly_id,
            display_name="月次 99.9% 可用性 SLO (許容 43.2 分/月)",
            goal=0.999,
            total_budget_minutes=MONTHLY_999_TOTAL_BUDGET_MINUTES,
        )
        if daily is None and monthly is None:
            return None
        return SloBudgetSnapshot(
            project_id=self._settings.project_id,
            service_id=self._settings.slo_service_id,
            daily_slo=daily,
            monthly_slo=monthly,
        )

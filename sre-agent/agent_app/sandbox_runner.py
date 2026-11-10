"""Cloud Run sandboxes 隔離コード実行ラッパー。

Cloud Run instances で `--sandbox-launcher` を有効化すると利用できる
`/usr/local/gcp/bin/sandbox do` コマンドを呼び出し、以下を強制した隔離空間で
メトリクス時系列の回帰分析やログ集計用の Python スクリプトを実行します。

セキュリティ境界:
  - 外向きネットワーク完全遮断（`--allow-egress` を付与しない）
  - 親コンテナの環境変数・Slack トークン・GCP メタデータ認証情報を一切引き継がない（`-e` 不使用）
  - ルートファイルシステムは読み取り専用（`--write` 不使用、`/tmp` のみメモリ上利用可）
  - 非対話実行のため `--stdin=false` とタイムアウト時の強制クリーンアップ（`sandbox delete --force`）を実装
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

from .config import AgentSettings
from .models import SandboxMetricsAnalysis

# 隔離サンドボックス内で実行する自己完結型 Python 解析スクリプト（外部通信・外部依存ゼロ）
_SERIES_ANALYSIS_SCRIPT = """
import json, sys

series = json.loads(sys.argv[1])
n = len(series)
if n == 0:
    print(json.dumps({
        "sample_count": 0,
        "peak_error_rate": 0.0,
        "latest_error_rate": 0.0,
        "linear_slope": 0.0,
        "is_naturally_decaying": False,
    }))
    sys.exit(0)

peak = max(series)
latest = series[-1]
if n >= 2:
    mean_x = (n - 1) / 2.0
    mean_y = sum(series) / n
    num = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(series))
    den = sum((i - mean_x) ** 2 for i in range(n))
    slope = num / den if den > 0 else 0.0
else:
    slope = 0.0

# ピーク後に負の傾きで減衰し、直近エラー率がピークの半分以下かつ 0.05 未満なら自然減衰中と判定
decaying = bool(n >= 3 and slope < -0.02 and latest <= peak * 0.5 and latest < 0.05)
print(json.dumps({
    "sample_count": n,
    "peak_error_rate": round(peak, 4),
    "latest_error_rate": round(latest, 4),
    "linear_slope": round(slope, 4),
    "is_naturally_decaying": decaying,
}))
""".strip()


class SandboxRunner:
    """隔離サンドボックス実行器。"""

    def __init__(self, settings: AgentSettings) -> None:
        self._settings = settings

    def build_command(self, python_code: str, arg_json: str, sandbox_id: str) -> list[str]:
        """実行モードに応じたコマンド配列を構築する（テストで直接検証可能）。"""
        if self._settings.sandbox_mode == "cloud_run" and Path(self._settings.sandbox_binary_path).exists():
            return [
                self._settings.sandbox_binary_path,
                "do",
                f"--sandbox-name={sandbox_id}",
                "--stdin=false",
                "--",
                "/usr/local/bin/python3" if Path("/usr/local/bin/python3").exists() else "/usr/bin/python3",
                "-I",
                "-c",
                python_code,
                arg_json,
            ]
        # ローカル開発・単体テスト環境フォールバック（-I 分離モード・空環境変数で実行）
        return [sys.executable, "-I", "-c", python_code, arg_json]

    def analyze_error_series(self, series: list[float], timeout_seconds: float = 10.0) -> SandboxMetricsAnalysis:
        """時系列エラー率データを隔離サンドボックス内で統計解析し、自然減衰傾向を判定する。"""
        sandbox_id = f"sre-stat-{uuid.uuid4().hex[:10]}"
        payload = json.dumps([float(x) for x in series])
        cmd = self.build_command(_SERIES_ANALYSIS_SCRIPT, payload, sandbox_id)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                # ローカルフォールバック時も親プロセスの秘密情報（トークン等）を一切渡さない
                env={"PATH": "/usr/local/bin:/usr/bin:/bin"} if cmd[0] == sys.executable else None,
            )
        except subprocess.TimeoutExpired as exc:
            self._force_cleanup_sandbox(sandbox_id)
            raise RuntimeError(f"隔離サンドボックスの実行がタイムアウトしました ({timeout_seconds}s)") from exc

        if proc.returncode != 0:
            raise RuntimeError(f"隔離サンドボックス解析エラー (code={proc.returncode}): {proc.stderr.strip()}")

        data = json.loads(proc.stdout.strip())
        return SandboxMetricsAnalysis.model_validate(data)

    def _force_cleanup_sandbox(self, sandbox_id: str) -> None:
        """タイムアウト時に残留サンドボックスを強制削除する（終了コード 1 は公式仕様どおり無視）。"""
        if self._settings.sandbox_mode == "cloud_run" and Path(self._settings.sandbox_binary_path).exists():
            subprocess.run(
                [self._settings.sandbox_binary_path, "delete", "--force", sandbox_id],
                capture_output=True,
                text=True,
                check=False,
            )

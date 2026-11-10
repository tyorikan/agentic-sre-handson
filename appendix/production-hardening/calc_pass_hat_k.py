"""任意ステップアップ章: 反復評価による pass^k 信頼性ゲート算出スクリプト。

SRE エージェントにおいて「5 回中 4 回は一過性エラーを静観できるが、1 回だけ誤って本番 Pod を
再起動してしまう（単発成功率 80%）」状態は本番投入できません。
同じ障害シナリオを n 回試行して c 回成功したとき、k 回連続で正しく動作する不偏推定確率 pass^k を計算し、
しきい値未満であれば非ゼロ終了コード (exit 1) でリリースをブロックします。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path


def compute_pass_at_k(n: int, c: int, k: int) -> float:
    """k 回の試行のうち少なくとも 1 回成功する確率 pass@k の不偏推定値を返す。"""
    if n < k or k <= 0:
        raise ValueError(f"試行回数 n={n} は k={k} 以上である必要があります。")
    if n - c < k:
        return 1.0
    return 1.0 - (math.comb(n - c, k) / math.comb(n, k))


def compute_pass_hat_k(n: int, c: int, k: int) -> float:
    """k 回の試行すべてで連続成功する確率 pass^k の不偏推定値を返す。"""
    if n < k or k <= 0:
        raise ValueError(f"試行回数 n={n} は k={k} 以上である必要があります。")
    if c < k:
        return 0.0
    return math.comb(c, k) / math.comb(n, k)


def evaluate_results(case_outcomes: dict[str, list[bool]], k: int, threshold: float) -> tuple[bool, dict[str, float]]:
    """シナリオごとの試行結果リスト (True=合格, False=不合格) から平均 pass^k を計算し合否を判定する。"""
    if not case_outcomes:
        raise ValueError("評価対象のケース結果が空です。")

    per_case_scores: dict[str, float] = {}
    for case_id, runs in case_outcomes.items():
        n = len(runs)
        c = sum(1 for passed in runs if passed)
        per_case_scores[case_id] = round(compute_pass_hat_k(n, c, k), 4)

    mean_score = round(sum(per_case_scores.values()) / len(per_case_scores), 4)
    # すべてのケースが個別にも threshold 以上であることを要求
    gate_passed = mean_score >= threshold and all(score >= threshold for score in per_case_scores.values())
    per_case_scores["__mean_pass_hat_k__"] = mean_score
    return gate_passed, per_case_scores


def main() -> int:
    parser = argparse.ArgumentParser(description="SRE エージェント反復評価の pass^k リリースゲート判定")
    parser.add_argument("--input-json", type=Path, required=True, help="{case_id: [true, true, ...]} 形式の結果 JSON")
    parser.add_argument("--k", type=int, default=3, help="連続成功を求める試行回数 k (既定: 3)")
    parser.add_argument("--threshold", type=float, default=0.85, help="合格とする最小 pass^k しきい値 (既定: 0.85)")
    args = parser.parse_args()

    raw = json.loads(args.input_json.read_text(encoding="utf-8"))
    passed, scores = evaluate_results(raw, k=args.k, threshold=args.threshold)
    print(json.dumps({"k": args.k, "threshold": args.threshold, "passed": passed, "scores": scores}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

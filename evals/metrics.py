"""评测指标（W2 · PR-114）：纯函数，无 IO、无网络，便于单测与 CI 复用。

指标口径（与 `docs/production-readiness/phase-1.md` §3 对齐）：
- 任务成功率：全部断言通过（结构/数字/拒答/时延）的用例占比；
- 工具选择：以「实际执行过的工具名集合」对比 `expected_tools`，给出 micro P/R/F1 与精确匹配率；
- grounding：`expected_grounding` 标签 vs `LoopResult.grounding_verified`，给出 P/R/F1；
- 延迟：p50 / p95 / max（线性插值分位，不依赖 numpy）；
- 成本：`cost_usd=None` 的调用单列，不并进 total 冒充完整成本。
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Iterable, Sequence

GROUNDING_LABELS = (True, False, None)


def percentile(values: Sequence[float], q: float) -> float | None:
    """线性插值分位数（q ∈ [0,1]）；空序列返回 None。"""
    data = sorted(float(v) for v in values)
    if not data:
        return None
    if q <= 0:
        return data[0]
    if q >= 1:
        return data[-1]
    pos = (len(data) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return data[lo]
    frac = pos - lo
    return data[lo] * (1 - frac) + data[hi] * frac


def latency_stats(values: Iterable[float]) -> dict[str, float | None]:
    """延迟统计（输入单位 ms，输出单位 ms）。"""
    data = [float(v) for v in values]
    if not data:
        return {"count": 0, "p50": None, "p95": None, "max": None, "mean": None}
    return {
        "count": len(data),
        "p50": round(percentile(data, 0.5) or 0.0, 3),
        "p95": round(percentile(data, 0.95) or 0.0, 3),
        "max": round(max(data), 3),
        "mean": round(sum(data) / len(data), 3),
    }


def _ratio(num: float, den: float) -> float | None:
    return round(num / den, 6) if den else None


def tool_scores(expected: Sequence[Iterable[str]], observed: Sequence[Iterable[str]]) -> dict[str, Any]:
    """工具选择 micro precision/recall/F1 + 精确匹配率。

    `expected` / `observed` 必须等长且逐用例对应；空集（如拒答用例）也参与评分：
    期望空、实际空 → 精确匹配；期望空、实际调了工具 → false positive（该扣分）。
    """
    if len(expected) != len(observed):
        raise ValueError(f"expected/observed length mismatch: {len(expected)} vs {len(observed)}")
    tp = fp = fn = 0
    exact = 0
    per_case: list[dict[str, Any]] = []
    for exp_raw, obs_raw in zip(expected, observed):
        exp = {str(x) for x in exp_raw}
        obs = {str(x) for x in obs_raw}
        case_tp = len(exp & obs)
        case_fp = len(obs - exp)
        case_fn = len(exp - obs)
        tp += case_tp
        fp += case_fp
        fn += case_fn
        matched = exp == obs
        exact += 1 if matched else 0
        per_case.append(
            {
                "expected": sorted(exp),
                "observed": sorted(obs),
                "missing": sorted(exp - obs),
                "unexpected": sorted(obs - exp),
                "exact": matched,
            }
        )
    total = len(expected)
    return {
        "cases": total,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
        "f1": _ratio(2 * tp, 2 * tp + fp + fn),
        "exact_match_rate": _ratio(exact, total),
        "per_case": per_case,
    }


def grounding_scores(labels: Sequence[bool | None], predictions: Sequence[bool]) -> dict[str, Any]:
    """grounding 标签 vs 预测；`None` 标签不计入（该用例不判 grounding）。"""
    if len(labels) != len(predictions):
        raise ValueError(f"labels/predictions length mismatch: {len(labels)} vs {len(predictions)}")
    tp = fp = fn = tn = 0
    for label, pred in zip(labels, predictions):
        if label is None:
            continue
        if label and pred:
            tp += 1
        elif label and not pred:
            fn += 1
        elif not label and pred:
            fp += 1
        else:
            tn += 1
    scored = tp + fp + fn + tn
    return {
        "scored": scored,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
        "f1": _ratio(2 * tp, 2 * tp + fp + fn),
    }


def failure_class(failures: Iterable[str]) -> str:
    """把失败码归类（与 evals/run.py 的分类口径保持一致）。"""
    codes = [str(f) for f in failures]
    if not codes:
        return "pass"
    joined = " ".join(codes)
    if "loop_exception" in joined or "not_terminated" in joined:
        return "loop_crash_or_hang"
    if "tool_not_registered" in joined or "unknown_structure" in joined or "golden_invalid" in joined:
        return "spec_invalid"
    if "latency_exceeded" in joined:
        return "latency"
    if "refusal_missing" in joined:
        return "safety_gap"
    if "no_data_not_declared" in joined:
        return "honesty_gap"
    if "unexpected_content" in joined:
        return "unsafe_content"
    if "content_missing" in joined:
        return "content_gap"
    return "other"


def aggregate(rows: Sequence[dict[str, Any]], *, quality: bool = True) -> dict[str, Any]:
    """汇总用例结果为报告指标。

    `quality=False`（fake 模式）时不输出工具/grounding/成本结论，只给
    「管道是否跑通」的统计——fake 只证明评测管道自洽，不证明模型质量。
    """
    total = len(rows)
    passed = sum(1 for r in rows if r.get("passed"))
    by_category: dict[str, dict[str, int]] = {}
    classes: Counter[str] = Counter()
    for row in rows:
        cat = str(row.get("category") or "unknown")
        bucket = by_category.setdefault(cat, {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += 1 if row.get("passed") else 0
        if not row.get("passed"):
            classes[str(row.get("failure_class") or "other")] += 1

    report: dict[str, Any] = {
        "total": total,
        "passed": passed,
        "pass_rate": _ratio(passed, total),
        "by_category": by_category,
        "latency_ms": latency_stats([float(r.get("elapsed_ms") or 0.0) for r in rows]),
        "failure_classes": dict(classes),
        "quality_metrics": None,
        "quality_note": "fake 模式只验证评测管道，不产出模型质量结论" if not quality else None,
    }
    if not quality:
        return report

    tool = tool_scores(
        [r.get("expected_tools") or [] for r in rows],
        [r.get("tools_called") or [] for r in rows],
    )
    grounding = grounding_scores(
        [r.get("expect_grounding") for r in rows],
        [bool(r.get("grounding_verified")) for r in rows],
    )
    costs = [r.get("cost_usd") for r in rows]
    priced = [float(c) for c in costs if c is not None]
    report["quality_metrics"] = {
        "tool_selection": {k: v for k, v in tool.items() if k != "per_case"},
        "tool_selection_cases": tool["per_case"],
        "grounding": grounding,
        "cost": {
            "total_usd": round(sum(priced), 8) if priced else None,
            "per_task_usd": round(sum(priced) / len(priced), 8) if priced else None,
            "priced_cases": len(priced),
            "unpriced_cases": total - len(priced),
            "cost_complete": total > 0 and len(priced) == total,
        },
    }
    return report


__all__ = [
    "GROUNDING_LABELS",
    "aggregate",
    "failure_class",
    "grounding_scores",
    "latency_stats",
    "percentile",
    "tool_scores",
]

"""定价回填桩（W4 · PR-112）。

背景：历史 run 的 cost 目前来自 llm/catalog.py 的单条当前价，没有版本化价目源，
无法按「调用发生日」重算历史账单。本模块先把调用面固定下来，阀门保持关闭：
恒返回 updated=0 / skipped=0 / reason=pricing_source_unavailable。

后续接真实价目源时只需要替换本函数体，调用方签名不变。
"""

from __future__ import annotations

from datetime import date
from typing import Any

REASON_PRICING_SOURCE_UNAVAILABLE = "pricing_source_unavailable"


def backfill_pricing(*, since: date | None = None, dry_run: bool = True) -> dict[str, Any]:
    """回填历史成本（桩）。

    since：只回填该日期之后的记录（None 表示全部）；dry_run：只统计不写库。
    当前没有可用价目源，两个参数都不改变结果：0 条更新、0 条跳过、原因稳定。
    """
    del since, dry_run  # 桩：参数只为固定调用面，尚无价目源可用
    return {"updated": 0, "skipped": 0, "reason": REASON_PRICING_SOURCE_UNAVAILABLE}


__all__ = ["REASON_PRICING_SOURCE_UNAVAILABLE", "backfill_pricing"]

"""PR-112 回填桩：返回 0 条且 reason 稳定（阀门关闭）。"""

from __future__ import annotations

from datetime import date

from hero_quant.pricing.backfill import REASON_PRICING_SOURCE_UNAVAILABLE, backfill_pricing


def test_backfill_stub_reports_zero_and_stable_reason():
    result = backfill_pricing()

    assert result == {"updated": 0, "skipped": 0, "reason": "pricing_source_unavailable"}
    assert result["reason"] == REASON_PRICING_SOURCE_UNAVAILABLE


def test_backfill_stub_accepts_window_and_dry_run_flags():
    result = backfill_pricing(since=date(2026, 1, 1), dry_run=False)

    assert result["updated"] == 0 and result["skipped"] == 0
    assert result["reason"] == REASON_PRICING_SOURCE_UNAVAILABLE

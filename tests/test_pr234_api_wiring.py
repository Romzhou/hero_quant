"""批次15：把 realism_report 接进回测产物（_backtest_realism / bundle / 缓存）。

口径：
- fills 由持仓市值差分反推，首根 bar 的建仓行整行跳过（无前收盘可比，不编 prev_close）；
- 成交量缺失时降级为 volume_missing，不猜停牌；
- 静态兜底 bundle 也必须带 realism.degraded + reason，不给空数字。
"""

from __future__ import annotations

import pandas as pd

from hero_quant.api import server as srv


def _frame(rows: int = 3, with_volume: bool = True) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=rows, freq="D")
    prices = pd.DataFrame({"close": [10.0, 10.5, 11.0][:rows]}, index=idx)
    prices.index.name = "date"
    if with_volume:
        vol = pd.Series([1000.0, 1100.0, 1200.0][:rows], index=idx)
        vol.index.name = "date"
        prices.attrs["volume"] = vol
    return prices


def _positions(prices: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"asset_0": [0.0, 0.5, 0.0]}, index=prices.index)


def test_realism_computes_trades_and_costs():
    prices = _frame()
    report, assumptions = srv._backtest_realism(_positions(prices), prices, 0.0005)
    assert report["degraded"] is False
    assert report["trades"] == 2  # 首根 bar 的建仓行被排除
    assert report["total_cost"] > 0
    assert assumptions["first_bar_fills_excluded"] is True
    assert assumptions["engine_costs"] == 0.0005


def test_realism_without_volume_degrades():
    prices = _frame(with_volume=False)
    report, _ = srv._backtest_realism(_positions(prices), prices, None)
    assert report["degraded"] is True
    assert report["reason"] == "volume_missing"
    assert report["trades"] is None


def test_realism_without_positions_degrades():
    prices = _frame()
    report, _ = srv._backtest_realism(None, prices, None)
    assert report["degraded"] is True
    assert report["reason"] == "positions_missing"


def test_static_bundle_has_degraded_realism():
    bundle = srv._static_backtest_bundle()
    assert bundle["realism"] == {"degraded": True, "reason": "static_bundle"}
    assert bundle["realism_assumptions"] is None


def test_bundle_and_cache_carry_realism():
    from pathlib import Path

    src = Path(srv.__file__).read_text(encoding="utf-8")
    assert '"realism": realism' in src
    assert '"realism_assumptions": realism_assumptions' in src
    # 缓存写入也要带，否则 L2 命中时字段凭空消失
    assert '"realism": bundle.get("realism")' in src

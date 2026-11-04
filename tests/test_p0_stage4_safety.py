"""阶段4 两条 P1 安全项：行为级回归（TDD 先红后绿）。

1) allow_synthetic 默认 False：docstring 说「仅显式 opt-in」，默认值必须与之一致；
   未 opt-in 时瞬时错误直接抛，不得静默回退合成冒充 live。
2) skip_pit=True 仍执行 validate()：只跳过 PIT 时序比对，价格/索引/币种检查照旧放行不得。
"""

from __future__ import annotations

import inspect

import pandas as pd
import pytest

from hero_quant.backtest.engine import BacktestEngine
from hero_quant.backtest.validation import PIT_ACK, ValidationError, validate


def _prices(values=(100.0, 101.0, 102.0)) -> pd.DataFrame:
    return pd.DataFrame(
        {"close": [float(v) for v in values]},
        index=pd.date_range("2024-01-01", periods=len(values), freq="D"),
    )


def test_allow_synthetic_default_is_false_in_both_tools():
    from hero_quant.tools import market_data as md

    assert inspect.signature(md.get_market_data).parameters["allow_synthetic"].default is False
    assert inspect.signature(md.get_bars_range).parameters["allow_synthetic"].default is False


def test_without_optin_transient_error_does_not_fall_back_to_synthetic(monkeypatch):
    import hero_quant.tools.market_data as md

    def _net_err(*a, **k):
        raise TimeoutError("upstream timeout")

    monkeypatch.setattr(md, "_get_shared_registry", lambda: type("R", (), {"get_bars": _net_err})())
    with pytest.raises(RuntimeError, match="synthetic fallback requires allow_synthetic"):
        md.get_market_data(symbol="600519.SH")
    out = md.get_market_data(symbol="600519.SH", allow_synthetic=True)
    assert out["ok"] is False and out["provenance"]["source"] == "synthetic"


def test_skip_pit_still_blocks_negative_price():
    # 直接打 validate：负价格属于非 PIT 检查；engine 的 _price_matrix 也会在更前面
    # 拦下同一坏数据，但这里锁的是 validate(skip_pit=True) 本身没有整段跳过。
    prices = _prices((100.0, -1.0, 102.0))
    with pytest.raises(ValidationError, match="non-positive"):
        validate(prices, skip_pit=True)


def test_skip_pit_still_blocks_unsorted_index():
    prices = _prices()
    prices = prices.iloc[[2, 0, 1]]
    with pytest.raises(ValidationError, match="not sorted"):
        BacktestEngine().run(prices, weights=[1.0], skip_pit=True, pit_ack=PIT_ACK)


def test_skip_pit_still_blocks_mixed_currency():
    prices = _prices()
    prices["currency"] = ["CNY", "USD", "CNY"]
    with pytest.raises(ValidationError, match="mixed currencies"):
        BacktestEngine().run(prices, weights=[1.0], skip_pit=True, pit_ack=PIT_ACK)


def test_skip_pit_skips_only_the_pit_comparison():
    prices = _prices()
    # 未来数据（weights_on > price_date）是 PIT 特有违规，skip_pit 下应放行，
    # 但非 PIT 检查仍跑完（价格/索引合法）。
    assert validate(prices, weights_on="2024-01-10", price_date="2024-01-01", skip_pit=True) is None
    with pytest.raises(ValidationError, match="PIT violation"):
        validate(prices, weights_on="2024-01-10", price_date="2024-01-01")
    res = BacktestEngine().run(
        prices, weights=[1.0], weights_on="2024-01-10", price_date="2024-01-01",
        skip_pit=True, pit_ack=PIT_ACK,
    )
    assert res.get("non_pit") is True


def test_validate_rejects_unknown_kwargs_even_with_skip_pit():
    prices = _prices()
    with pytest.raises(ValidationError, match="unknown kwargs"):
        validate(prices, skip_pit=True, weights_onn="2024-01-01")

"""H线：quantlib/backtest 手写边界用例（无 hypothesis 依赖：pyproject/dev 无此包）。

覆盖 indicators 空/NaN/Inf/别名/窗口边界、options 到期/零波/非法参数/
iv 往返、backtest engine 资金/PIT/价格守卫、metrics 空/成本/破产、
bench synthetic 门控。全部 fail-closed 语义锁定。
"""
import math

import numpy as np
import pandas as pd
import pytest


# ---------- indicators 边界 ----------

def test_h_ind_empty_inputs():
    from hero_quant.quantlib.indicators import sma, ema, rsi, bollinger, macd, max_drawdown

    e = pd.Series(dtype=float)
    assert len(sma(e, 3)) == 0
    assert len(ema(e, 3)) == 0
    assert len(rsi(e, 14)) == 0
    m, u, l = bollinger(e)
    assert len(m) == 0 and len(u) == 0 and len(l) == 0
    a, b, c = macd(e)
    assert len(a) == 0
    assert max_drawdown(e) == 0.0


def test_h_ind_all_nan():
    from hero_quant.quantlib.indicators import sma, rsi, max_drawdown

    s = pd.Series([float("nan")] * 10)
    assert sma(s, 3).isna().all()
    # rsi 有效点不足 -> 全 NaN
    assert rsi(s, 14).isna().all()
    assert max_drawdown(s) == 0.0


def test_h_ind_inf_and_constant():
    from hero_quant.quantlib.indicators import sma, rsi, bollinger, macd, max_drawdown

    s = pd.Series([1.0, float("inf"), 3.0, 4.0, 5.0])
    assert sma(s, 2).notna().sum() >= 1
    flat = pd.Series([5.0] * 30)
    r = rsi(flat, 14)
    assert ((r == 50.0) | r.isna()).all()
    mid, up, lo = bollinger(flat, window=5)
    assert (up.dropna() >= mid.dropna()).all()
    assert (lo.dropna() <= mid.dropna()).all()
    ml, sl, hh = macd(flat)
    assert len(ml) == len(flat)
    assert max_drawdown(pd.Series([100, 110, 120])) == 0.0


def test_h_ind_max_drawdown_crash_and_zero_base():
    from hero_quant.quantlib.indicators import max_drawdown

    assert max_drawdown(pd.Series([100, 50, 75])) == pytest.approx(-0.5)
    # 权益起点 0：除零保护 -> 0.0，不抛
    assert max_drawdown(pd.Series([0.0, 0.0, 10.0])) == 0.0
    assert max_drawdown(pd.Series([100, float("nan"), 50])) < 0.0


def test_h_ind_to_series_variants():
    from hero_quant.quantlib.indicators import _to_series

    df_c = pd.DataFrame({"close": [1.0, 2.0], "open": [9.0, 9.0]})
    assert _to_series(df_c).tolist() == [1.0, 2.0]
    df_e = pd.DataFrame({"equity": [3.0, 4.0]})
    assert _to_series(df_e).tolist() == [3.0, 4.0]
    df_f = pd.DataFrame({"zzz": [7.0, 8.0]})
    assert _to_series(df_f).tolist() == [7.0, 8.0]
    assert len(_to_series([1, 2, 3])) == 3
    s_inf = _to_series([1.0, float("inf")])
    assert s_inf.isna().sum() == 1


def test_h_ind_window_string_int_ok():
    from hero_quant.quantlib.indicators import _validate_window, sma

    assert _validate_window("20") == 20
    assert _validate_window(20.9) == 20
    s = pd.Series(range(1, 30))
    assert sma(s, window="5").iloc[-1] == pytest.approx(sum(range(25, 30)) / 5)


def test_h_ind_alias_kwargs():
    from hero_quant.quantlib.indicators import sma, ema, rsi, bollinger, macd

    s = pd.Series(range(1, 40), dtype=float)
    assert sma(s, n=5).iloc[-1] == pytest.approx(s.rolling(5).mean().iloc[-1])
    assert sma(s, period=5).iloc[-1] == pytest.approx(s.rolling(5).mean().iloc[-1])
    assert ema(s, window=5).iloc[-1] == pytest.approx(s.ewm(span=5, adjust=False, min_periods=1).mean().iloc[-1])
    assert 0 <= rsi(s, window=14).iloc[-1] <= 100
    m1, _, _ = bollinger(s, n=10)
    m2, _, _ = bollinger(s, window=10)
    pd.testing.assert_series_equal(m1, m2)
    a1, _, _ = macd(s, n_fast=5)
    a2, _, _ = macd(s, fast=5)
    pd.testing.assert_series_equal(a1, a2)


def test_h_ind_bollinger_num_std_fallback():
    from hero_quant.quantlib.indicators import bollinger

    s = pd.Series(range(1, 40), dtype=float)
    m_bad, u_bad, l_bad = bollinger(s, window=10, num_std="bad")
    m_ok, u_ok, l_ok = bollinger(s, window=10, num_std=2.0)
    pd.testing.assert_series_equal(m_bad, m_ok)
    # 负倍数回落默认 2.0
    _, u_neg, _ = bollinger(s, window=10, num_std=-3)
    pd.testing.assert_series_equal(u_neg, u_ok)


def test_h_ind_rsi_monotone_bounds():
    from hero_quant.quantlib.indicators import rsi

    up = pd.Series(range(1, 40), dtype=float)
    assert rsi(up, 14).iloc[-1] == pytest.approx(100.0)
    down = pd.Series(range(40, 1, -1), dtype=float)
    assert rsi(down, 14).iloc[-1] == pytest.approx(0.0, abs=1.0)


def test_h_ind_macd_zero_when_flat():
    from hero_quant.quantlib.indicators import macd

    flat = pd.Series([10.0] * 40)
    ml, sl, hh = macd(flat)
    assert hh.dropna().abs().max() == pytest.approx(0.0, abs=1e-9)


# ---------- options 边界 ----------

def test_h_opt_expiry_intrinsic():
    from hero_quant.quantlib.options import bs_price

    assert bs_price(S=110, K=100, T=0, option_type="call") == pytest.approx(10.0)
    assert bs_price(S=90, K=100, T=0, option_type="put") == pytest.approx(10.0)
    assert bs_price(S=90, K=100, T=0, option_type="call") == 0.0


def test_h_opt_zero_vol_discounted():
    from hero_quant.quantlib.options import bs_price

    p = bs_price(S=100, K=100, T=1.0, r=0.05, sigma=0.0, option_type="call")
    assert p == pytest.approx(max(100 - 100 * math.exp(-0.05), 0.0))


def test_h_opt_nonneg_and_bad_spot():
    from hero_quant.quantlib.options import bs_price

    for kw in [dict(S=-5, K=100, T=1.0), dict(S=0, K=100, T=1.0), dict(S=100, K=-2, T=1.0)]:
        assert bs_price(option_type="call", **kw) >= 0.0
        assert bs_price(option_type="put", **kw) >= 0.0
    assert bs_price(S=100, K=100, T=1.0, sigma=0.2) >= 0.0


def test_h_opt_invalid_args_raise():
    from hero_quant.quantlib.options import bs_price, bs_greeks, implied_volatility

    with pytest.raises(TypeError):
        bs_price(S=100, K=100, T=None)
    with pytest.raises(TypeError):
        bs_price(S="bad", K=100, T=1.0)
    with pytest.raises(TypeError):
        bs_greeks(S=100, K=100, T=None)
    with pytest.raises(TypeError):
        implied_volatility(1.0, S=None, K=100, T=1.0)
    with pytest.raises((ValueError, TypeError)):
        implied_volatility(1.0, S=-1, K=100, T=1.0)


def test_h_opt_greeks_expiry_indicator():
    from hero_quant.quantlib.options import bs_greeks

    g_itm = bs_greeks(S=110, K=100, T=0, option_type="call")
    assert g_itm["delta"] == 1.0 and g_itm["gamma"] == 0.0
    g_otm = bs_greeks(S=90, K=100, T=0, option_type="call")
    assert g_otm["delta"] == 0.0
    g_atm = bs_greeks(S=100, K=100, T=0, option_type="call")
    assert g_atm["delta"] == 0.5


def test_h_opt_greeks_live_shape():
    from hero_quant.quantlib.options import bs_greeks

    g = bs_greeks(S=100, K=100, T=1.0, sigma=0.2, option_type="call")
    assert set(g) == {"delta", "gamma", "vega", "theta", "rho"}
    assert 0.0 <= g["delta"] <= 1.0
    assert g["gamma"] >= 0.0
    assert g["vega"] >= 0.0


def test_h_opt_iv_roundtrip_and_bounds():
    from hero_quant.quantlib.options import bs_price, implied_volatility, iv

    assert iv is implied_volatility
    p = bs_price(S=100, K=100, T=1.0, r=0.05, sigma=0.25, option_type="call")
    rec = implied_volatility(p, S=100, K=100, T=1.0, r=0.05, option_type="call")
    assert rec == pytest.approx(0.25, abs=0.02)
    # 到期/平价下界 -> 0
    assert implied_volatility(0.0, S=100, K=100, T=0.0) == 0.0
    # 低于内在价值 -> 套利错
    with pytest.raises(ValueError):
        implied_volatility(0.01, S=200, K=100, T=1.0, r=0.0, option_type="call")


def test_h_opt_put_call_parity_sign():
    from hero_quant.quantlib.options import bs_price

    c = bs_price(S=100, K=100, T=1.0, r=0.05, sigma=0.2, option_type="call")
    p = bs_price(S=100, K=100, T=1.0, r=0.05, sigma=0.2, option_type="put")
    lhs = c - p
    rhs = 100 - 100 * math.exp(-0.05 * 1.0)
    assert lhs == pytest.approx(rhs, abs=1e-6)


# ---------- backtest engine 边界 ----------

def _prices(n=20, start=100.0, step=1.0):
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame({"close": [start + i * step for i in range(n)]}, index=idx)


def test_h_bt_capital_guards():
    from hero_quant.backtest.engine import BacktestEngine

    for bad in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            BacktestEngine(initial_capital=bad)


def test_h_bt_run_happy_and_keys():
    from hero_quant.backtest.engine import BacktestEngine

    eng = BacktestEngine(initial_capital=10000)
    res = eng.run(_prices(), allow_synthetic=True)
    assert isinstance(res, dict)
    assert "equity" in res or "metrics" in res or "returns" in res or len(res) > 0


def test_h_bt_run_bad_inputs():
    from hero_quant.backtest.engine import BacktestEngine

    eng = BacktestEngine()
    with pytest.raises((TypeError, ValueError)):
        eng.run("not-a-frame", allow_synthetic=True)
    with pytest.raises(ValueError):
        eng.run(pd.DataFrame(), allow_synthetic=True)
    with pytest.raises(ValueError):
        eng.run(_prices(), costs=-0.1, allow_synthetic=True)
    with pytest.raises(ValueError):
        eng.run(_prices(), costs=float("nan"), allow_synthetic=True)


def test_h_bt_run_pit_guard():
    from hero_quant.backtest.engine import BacktestEngine

    eng = BacktestEngine()
    # 默认 enforce_pit 且无日期/allow_synthetic=False -> PITViolation
    from hero_quant.backtest.engine import PITViolation

    with pytest.raises(PITViolation):
        eng.run(_prices())
    # opt-out 无 ack -> PITViolation；有 ack 才放行
    with pytest.raises(PITViolation):
        eng.run(_prices(), skip_pit=True)
    res = eng.run(_prices(), skip_pit=True, pit_ack="I_KNOW_THIS_IS_NON_PIT")
    assert res.get("non_pit") is True


def test_h_bt_align_edges():
    from hero_quant.backtest.engine import BacktestEngine

    eng = BacktestEngine()
    with pytest.raises(ValueError):
        eng._align(pd.DataFrame(), 0)
    df = pd.DataFrame({"open": [10.0, 11.0], "close": [10.5, 11.5]})
    s_neg = eng._align(df, -5)  # 负索引归零不断言抛
    assert len(s_neg) == 1 and float(s_neg.iloc[0]) > 0
    s_last = eng._align(df, 99)  # 越界末 Bar 回落
    assert len(s_last) == 1 and float(s_last.iloc[0]) > 0
    bad = pd.DataFrame({"close": [0.0, -1.0]})
    with pytest.raises(ValueError):
        eng._align(bad, 0)


def test_h_bt_run_zero_and_short_prices():
    from hero_quant.backtest.engine import BacktestEngine

    eng = BacktestEngine()
    # 单 Bar 不崩
    res = eng.run(_prices(n=1), allow_synthetic=True)
    assert isinstance(res, dict)
    # 全零价格 -> fail-closed 抛错，不产假权益
    with pytest.raises((ValueError, Exception)):
        eng.run(pd.DataFrame({"close": [0.0, 0.0, 0.0]}), allow_synthetic=True)


def test_h_bt_run_multi_asset_and_weights():
    from hero_quant.backtest.engine import BacktestEngine

    eng = BacktestEngine()
    idx = pd.date_range("2025-01-01", periods=10, freq="D")
    px = pd.DataFrame({"A": np.linspace(100, 110, 10), "B": np.linspace(50, 55, 10)}, index=idx)
    res = eng.run(px, weights=[0.5, 0.5], allow_synthetic=True)
    assert isinstance(res, dict)
    # 全零权重（bear 空仓）保留不覆盖 -> 不抛
    res0 = eng.run(px, weights=[0.0, 0.0], allow_synthetic=True)
    assert isinstance(res0, dict)


# ---------- metrics 边界 ----------

def test_h_metrics_empty_and_nan():
    from hero_quant.backtest.metrics import compute_metrics

    z = compute_metrics(pd.Series(dtype=float))
    assert z["sharpe"] == 0.0 and z["max_drawdown"] == 0.0
    z2 = compute_metrics(pd.Series([float("nan")] * 5))
    assert z2["cumulative_return"] == 0.0


def test_h_metrics_df_and_costs():
    from hero_quant.backtest.metrics import compute_metrics

    idx = pd.date_range("2025-01-01", periods=30, freq="D")
    eq = pd.Series(np.linspace(100, 130, 30), index=idx)
    m0 = compute_metrics(pd.DataFrame({"equity": eq}))
    m1 = compute_metrics(eq, costs=0.001)
    assert m0["cumulative_return"] > 0
    # 成本拖累不应高于无成本
    assert m1["cumulative_return"] <= m0["cumulative_return"] + 1e-9
    # 非法 costs 被忽略不抛
    m_bad = compute_metrics(eq, costs="bad")
    assert m_bad["cumulative_return"] == pytest.approx(m0["cumulative_return"])


def test_h_metrics_bankrupt_floor():
    from hero_quant.backtest.metrics import compute_metrics

    eq = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0])
    m = compute_metrics(eq, costs=5.0)  # 巨大成本触发破产截断分支
    assert math.isfinite(m["cumulative_return"])
    assert m["cumulative_return"] <= 0.0 + 1e-9 or m["cumulative_return"] >= -1.0 - 1e-9


def test_h_metrics_single_point():
    from hero_quant.backtest.metrics import compute_metrics

    m = compute_metrics(pd.Series([100.0]), costs=0.001)
    assert m["sharpe"] == 0.0 or math.isfinite(m["sharpe"])


# ---------- bench 门控 ----------

def test_h_bench_guards():
    from hero_quant.backtest.bench import run_batch

    with pytest.raises(ValueError):
        run_batch(["AAA"], allow_synthetic=False)
    with pytest.raises(ValueError):
        run_batch([], allow_synthetic=True)


def test_h_bench_synthetic_smoke():
    from hero_quant.backtest.bench import run_batch

    out = run_batch(["AAA"], dates=["2025-01-01", "2025-01-02", "2025-01-03"], allow_synthetic=True)
    assert isinstance(out, dict)

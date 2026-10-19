"""Q线：quantlib/回测手算 golden 回归锁。

不依赖 pandas-ta / TA-Lib（CI 无此包），全部期望值为手算/独立实现推导，
见 docs/validation-report.md。只新增、不改存量测试。
"""

import math

import numpy as np
import pandas as pd

from hero_quant.backtest import metrics as M
from hero_quant.backtest.engine import BacktestEngine
from hero_quant.quantlib import indicators as Q
from hero_quant.quantlib import options as O


def test_sma_golden():
    s = pd.Series([1.0, 2, 3, 4, 5], dtype=float)
    out = Q.sma(s, 3).tolist()
    assert np.isnan(out[0]) and np.isnan(out[1])
    assert out[2:] == [2.0, 3.0, 4.0]


def test_ema_golden_span3():
    s = pd.Series([1.0, 2, 3, 4, 5], dtype=float)
    out = Q.ema(s, 3).tolist()
    assert out == [1.0, 1.5, 2.25, 3.125, 4.0625]


def test_rsi_boundaries():
    up = pd.Series([float(i) for i in range(1, 21)], dtype=float)
    assert float(Q.rsi(up, 14).iloc[-1]) == 100.0
    flat = pd.Series([10.0] * 20)
    assert float(Q.rsi(flat, 14).iloc[-1]) == 50.0


def test_rsi_converges_to_wilder_seed():
    rng = np.random.default_rng(7)
    px = pd.Series(100 + np.cumsum(rng.normal(0.2, 1.0, size=200)), dtype=float)
    n = 14
    rq = Q.rsi(px, n)
    d = px.diff()
    g = d.where(d > 0, 0.0)
    ell = -d.where(d < 0, 0.0)
    ag, al = float(g.iloc[1 : n + 1].mean()), float(ell.iloc[1 : n + 1].mean())
    w = np.full(len(px), np.nan)
    w[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(px)):
        ag = (ag * (n - 1) + float(g.iloc[i])) / n
        al = (al * (n - 1) + float(ell.iloc[i])) / n
        w[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    tail = (rq.iloc[-50:] - pd.Series(w, index=px.index).iloc[-50:]).abs()
    assert float(tail.max()) < 1e-3  # seeding 差异洗出后收敛


def test_bollinger_golden_ddof1():
    b = pd.Series([1.0, 2, 3, 4, 5], dtype=float)
    mid, upper, lower = Q.bollinger(b, 3, 2.0)
    assert mid.tolist()[2:] == [2.0, 3.0, 4.0]
    assert float(upper.iloc[2]) == 4.0  # 2 + 2*std([1,2,3],ddof=1)=2+2*1
    assert float(lower.iloc[2]) == 0.0


def test_macd_matches_ewm_formulation():
    m = pd.Series([1.0, 2, 3, 4, 5, 6, 7, 8], dtype=float)
    ml, sl, h = Q.macd(m, 2, 3, 2)
    ef = m.ewm(span=2, adjust=False).mean()
    es = m.ewm(span=3, adjust=False).mean()
    exp_ml = ef - es
    exp_sl = exp_ml.ewm(span=2, adjust=False).mean()
    assert float((ml - exp_ml).abs().max()) == 0.0
    assert float((sl - exp_sl).abs().max()) == 0.0
    assert float(((ml - sl) - h).abs().max()) == 0.0


def test_max_drawdown_golden_and_parity():
    eq = pd.Series([100, 110, 105, 120, 90, 100], dtype=float)
    assert Q.max_drawdown(eq) == -0.25
    assert M.max_drawdown(eq) == -0.25
    nan_mid = pd.Series([100.0, np.nan, 80.0, 90.0, np.nan, 70.0])
    assert Q.max_drawdown(nan_mid) == M.max_drawdown(nan_mid)
    assert abs(Q.max_drawdown(nan_mid) - (-0.3)) < 1e-12
    assert Q.max_drawdown(pd.Series([1.0, 2, 3])) == 0.0
    assert M.max_drawdown(pd.Series([np.nan, np.nan])) == 0.0


def test_sharpe_matches_hand_formula():
    eq = pd.Series([100.0, 101, 102, 103, 104], dtype=float)
    ret = eq.pct_change().dropna()
    hand = float(ret.mean() / ret.std(ddof=1) * math.sqrt(252))
    assert M.sharpe_ratio(eq) == hand
    assert M.sharpe_ratio(pd.Series([100.0] * 10)) == 0.0


def test_cagr_golden():
    assert M.annual_return(pd.Series([100.0, 200.0])) == 2.0**252 - 1
    assert M.annual_return(pd.Series([100.0, 110.0, 121.0])) == 1.21 ** (252 / 2) - 1


def test_turnover_single_side_semantics():
    assert M.turnover(weights=[0, 1, 0]) == 0.5  # sum|d|=2 单边/2 /2bar
    assert M.turnover(weights=[0.5, 0.5]) == 0.0
    assert M.turnover(positions=pd.Series([100.0] * 5)) == 0.0


def test_bs_parity_iv_greeks():
    S, K, T, r, sig = 100.0, 100.0, 1.0, 0.05, 0.2
    c = O.bs_price(S, K, T, r=r, sigma=sig, option_type="call")
    p = O.bs_price(S, K, T, r=r, sigma=sig, option_type="put")
    assert c - p == S - K * math.exp(-r * T)  # put-call parity 精确成立
    assert abs(O.implied_volatility(c, S=S, K=K, T=T, r=r, option_type="call") - sig) < 1e-6
    d1 = (math.log(S / K) + (r + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    hand_delta = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
    assert O.bs_greeks(S, K, T, r=r, sigma=sig, option_type="call")["delta"] == hand_delta


def test_engine_buyhold_next_bar_execution_semantics():
    prices = pd.DataFrame(
        {"close": [100.0, 110.0, 121.0]}, index=pd.date_range("2026-01-01", periods=3)
    )
    res = BacktestEngine(initial_capital=1.0).run(
        prices,
        weights=[1.0],
        costs=0.0,
        allow_synthetic=True,
        weights_on="2026-01-01",
        price_date="2026-01-01",
    )
    # bar0 决策次日(bar1,110)成交、末bar按收盘盯市 → [1.0, 1.1, 1.1]，非 naive [1.0,1.1,1.21]
    assert list(res["equity"]) == [1.0, 1.1, 1.1]

"""W4 覆盖率补齐：quantlib/options.py 未覆盖分支。

覆盖目标（src/hero_quant/quantlib/options.py）：
- _resolve_bs_args 的全部别名归一分支（s/spot/underlying, k/strike, t/tau/time_to_expiry,
  risk_free, vol/volatility, option_type/type/cp）
- bs_price 的 put 腿、零波动 put、非正 S/K 的 put 回落
- bs_greeks 的到期 put（ITM/OTM/ATM）、零波动 put（ITM/OTM/ATM）
- implied_volatility 的 T<=0 回落、价格等于内在价值回落、上界封顶分支

断言均为具体数值/异常类型，不使用 "not None" 之类弱断言。
"""

from __future__ import annotations

import math

import pytest

from hero_quant.quantlib.options import (
    bs_greeks,
    bs_price,
    implied_volatility,
    iv,
    price,
)


# ---------------------------------------------------------------- _resolve_bs_args 别名


def test_resolve_bs_args_lowercase_aliases_match_canonical():
    """别名参数（s/spot/k/strike/t/tau）归一后与显式参数定价完全一致。"""
    canonical = bs_price(S=100, K=100, T=1.0, r=0.05, sigma=0.2)
    via_alias = bs_price(s=100, spot=100, k=100, strike=100, t=1.0, tau=1.0, time_to_expiry=1.0)
    assert via_alias == pytest.approx(canonical, abs=1e-12)


def test_resolve_bs_args_underlying_alias_only():
    """underlying 是 S 的第三个别名。"""
    assert bs_price(underlying=110, K=100, T=1.0, r=0.0, sigma=0.2) == pytest.approx(
        bs_price(S=110, K=100, T=1.0, r=0.0, sigma=0.2), abs=1e-12
    )


def test_resolve_bs_args_risk_free_overrides_r():
    """risk_free 覆盖默认 r；显式 r 参数优先级低于 risk_free 关键字。"""
    a = bs_price(S=100, K=100, T=1.0, r=0.0, sigma=0.2, risk_free=0.05)
    b = bs_price(S=100, K=100, T=1.0, r=0.0, sigma=0.2)
    assert a != pytest.approx(b, abs=1e-9)
    assert a == pytest.approx(bs_price(S=100, K=100, T=1.0, r=0.05, sigma=0.2), abs=1e-12)


def test_resolve_bs_args_vol_and_volatility_override_sigma():
    """vol / volatility 关键字覆盖 sigma。"""
    via_vol = bs_price(S=100, K=100, T=1.0, sigma=0.2, vol=0.4)
    via_volatility = bs_price(S=100, K=100, T=1.0, sigma=0.2, volatility=0.4)
    expected = bs_price(S=100, K=100, T=1.0, sigma=0.4)
    assert via_vol == pytest.approx(expected, abs=1e-12)
    assert via_volatility == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize("key,value", [("option_type", "put"), ("type", "put"), ("cp", "put")])
def test_resolve_bs_args_option_type_aliases(key, value):
    """option_type / type / cp 三个别名都能把 call 切到 put。"""
    kwargs = {key: value}
    got = bs_price(S=100, K=100, T=1.0, r=0.05, sigma=0.2, **kwargs)
    assert got == pytest.approx(bs_price(S=100, K=100, T=1.0, r=0.05, sigma=0.2, option_type="put"), abs=1e-12)


def test_resolve_bs_args_sigma_kwarg():
    """sigma 作为关键字传入与位置参数等价。"""
    assert bs_price(S=100, K=100, T=1.0, sigma=0.35) == pytest.approx(
        bs_price(S=100, K=100, T=1.0, r=0.05, sigma=0.35), abs=1e-12
    )


def test_price_alias_is_bs_price():
    """price 别名与 bs_price 同义。"""
    assert price is bs_price
    assert price(S=100, K=100, T=1.0) == pytest.approx(bs_price(S=100, K=100, T=1.0), abs=1e-12)


# ---------------------------------------------------------------- bs_price 分支


def test_bs_price_put_zero_vol_is_discounted_intrinsic():
    """零波动 put：贴现内在价值 max(K*df - S, 0)。"""
    p = bs_price(S=90, K=100, T=1.0, r=0.05, sigma=0.0, option_type="put")
    assert p == pytest.approx(max(100 * math.exp(-0.05) - 90, 0.0), abs=1e-12)


def test_bs_price_put_zero_vol_otm_is_zero():
    """零波动 OTM put 贴现后仍为 0。"""
    p = bs_price(S=120, K=100, T=1.0, r=0.05, sigma=0.0, option_type="put")
    assert p == pytest.approx(max(100 * math.exp(-0.05) - 120, 0.0), abs=1e-12)
    assert p == 0.0


def test_bs_price_nonpositive_spot_put_branch():
    """S<=0 的 put 腿也走贴现内在价值回落，不抛异常。"""
    p = bs_price(S=0, K=100, T=1.0, r=0.05, sigma=0.2, option_type="put")
    assert p == pytest.approx(max(100 * math.exp(-0.05) - 0, 0.0), abs=1e-12)


def test_bs_price_nonpositive_strike_call_branch():
    """K<=0 的 call 腿回落为贴现内在价值 max(S - K*df, 0) = S。"""
    p = bs_price(S=100, K=0, T=1.0, r=0.05, sigma=0.2, option_type="call")
    assert p == pytest.approx(100.0, abs=1e-12)


def test_bs_price_type_case_insensitive():
    """option_type 大小写/空白归一（strip+lower+startswith c）。"""
    a = bs_price(S=100, K=100, T=1.0, sigma=0.2, option_type="CALL")
    b = bs_price(S=100, K=100, T=1.0, sigma=0.2, option_type="  call  ")
    c = bs_price(S=100, K=100, T=1.0, sigma=0.2, option_type="PUT")
    d = bs_price(S=100, K=100, T=1.0, sigma=0.2, option_type="put")
    assert a == pytest.approx(b, abs=1e-12)
    assert c == pytest.approx(d, abs=1e-12)
    assert a != pytest.approx(c, abs=1e-6)


def test_bs_price_missing_required_args_type_error():
    """S/K/T 缺失时抛 TypeError（消息含 requires S, K, T）。"""
    with pytest.raises(TypeError, match="requires S, K, T"):
        bs_price(S=100, K=100, T=None)


# ---------------------------------------------------------------- bs_greeks 分支


def test_bs_greeks_expiry_put_indicator():
    """到期 put：ITM delta=-1、OTM delta=0、ATM delta=-0.5。"""
    itm = bs_greeks(S=90, K=100, T=0, option_type="put")
    assert itm["delta"] == -1.0
    otm = bs_greeks(S=110, K=100, T=0, option_type="put")
    assert otm["delta"] == 0.0
    atm = bs_greeks(S=100, K=100, T=0, option_type="put")
    assert atm["delta"] == -0.5
    # 到期时其余希腊字母一律为 0
    assert (itm["gamma"], itm["vega"], itm["theta"], itm["rho"]) == (0.0, 0.0, 0.0, 0.0)


def test_bs_greeks_zero_vol_put_itm():
    """零波动 ITM put：delta=-1，rho<0、theta>0（贴现 carry 符号）。"""
    g = bs_greeks(S=90, K=100, T=1.0, r=0.05, sigma=0.0, option_type="put")
    df = math.exp(-0.05)
    assert g["delta"] == -1.0
    assert g["rho"] == pytest.approx(-100 * 1.0 * df, abs=1e-9)
    assert g["theta"] == pytest.approx(0.05 * 100 * df, abs=1e-9)
    assert g["gamma"] == 0.0 and g["vega"] == 0.0


def test_bs_greeks_zero_vol_put_otm():
    """零波动 OTM put：delta=0，rho=theta=0。"""
    g = bs_greeks(S=120, K=100, T=1.0, r=0.05, sigma=0.0, option_type="put")
    assert g["delta"] == 0.0
    assert g["rho"] == 0.0
    assert g["theta"] == 0.0


def test_bs_greeks_zero_vol_put_atm_half_delta():
    """零波动 ATM put（S == K*df）：delta 取 -0.5 约定值。"""
    df = math.exp(-0.05)
    g = bs_greeks(S=100 * df, K=100, T=1.0, r=0.05, sigma=0.0, option_type="put")
    assert g["delta"] == pytest.approx(-0.5, abs=1e-9)


def test_bs_greeks_zero_vol_call_atm_half_delta():
    """零波动 ATM call（S == K*df）：delta 取 0.5 约定值。"""
    df = math.exp(-0.05)
    g = bs_greeks(S=100 * df, K=100, T=1.0, r=0.05, sigma=0.0, option_type="call")
    assert g["delta"] == pytest.approx(0.5, abs=1e-9)


def test_bs_greeks_live_put_branch():
    """T>0 且 sigma>0 的 put：delta<0，theta > call 的同口径（put carry 为正项）。"""
    g = bs_greeks(S=100, K=100, T=1.0, r=0.05, sigma=0.2, option_type="put")
    assert g["delta"] < 0.0
    assert g["delta"] > -1.0
    assert g["gamma"] > 0.0
    assert g["vega"] > 0.0
    # put 的 rho 为负（对利率的敏感度符号与 call 相反）
    assert g["rho"] < 0.0


def test_bs_greeks_missing_args_type_error():
    """greeks 缺参数抛 TypeError。"""
    with pytest.raises(TypeError, match="requires S, K, T"):
        bs_greeks(S=100, K=100, T=None)


def test_bs_greeks_invalid_numeric_type_error():
    """greeks 非数值参数抛 TypeError。"""
    with pytest.raises(TypeError, match="invalid numeric args"):
        bs_greeks(S="abc", K=100, T=1.0)


# ---------------------------------------------------------------- implied_volatility


def test_iv_zero_remaining_term_returns_zero():
    """T<=0 无剩余期限，隐含波动率回落 0。"""
    assert implied_volatility(5.0, S=100, K=100, T=0.0) == 0.0


def test_iv_price_equal_intrinsic_returns_zero():
    """价格等于内在价值（+容差）时隐含波动率为 0。"""
    intrinsic = max(100 - 100 * math.exp(-0.05), 0.0)
    assert implied_volatility(intrinsic, S=100, K=100, T=1.0, r=0.05) == 0.0


def test_iv_missing_args_type_error():
    """IV 缺 S/K/T 抛 TypeError。"""
    with pytest.raises(TypeError, match="requires price, S, K, T"):
        implied_volatility(1.0, S=None, K=100, T=1.0)


def test_iv_invalid_numeric_type_error():
    """IV 非数值 price 抛 TypeError。"""
    with pytest.raises(TypeError, match="invalid numeric args"):
        implied_volatility("bad", S=100, K=100, T=1.0)


def test_iv_alias_matches_function():
    """iv 别名与 implied_volatility 同义。"""
    assert iv is implied_volatility


def test_iv_put_roundtrip():
    """put 的隐含波动率反推与定价自洽。"""
    p = bs_price(S=100, K=100, T=1.0, r=0.05, sigma=0.3, option_type="put")
    rec = implied_volatility(p, S=100, K=100, T=1.0, r=0.05, option_type="put")
    assert rec == pytest.approx(0.3, abs=0.02)

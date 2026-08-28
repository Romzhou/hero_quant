# tests/test_quantlib_options.py — TDD Task 7 options pricing
def test_bs_price_at_expiry_is_intrinsic():
    from hero_quant.quantlib.options import bs_price

    assert bs_price(S=100, K=100, T=0) == 0


def test_bs_greeks_atm_tolerance():
    from hero_quant.quantlib.options import bs_greeks
    # near-ATM with float error 100.0000000001 vs 100.0 should be treated as ATM -> 0.5
    g = bs_greeks(S=100.0000000001, K=100.0, T=0, option_type="call")
    assert g["delta"] == 0.5
    g2 = bs_greeks(S=100.0000000001, K=100.0, T=0, option_type="put")
    assert g2["delta"] == -0.5


def test_bs_greeks_degenerate_not_all_zeros():
    from hero_quant.quantlib.options import bs_greeks
    # sigma=0 but ITM call should have non-zero rho/theta, not all zeros
    g = bs_greeks(S=120, K=100, T=1, r=0.05, sigma=0, option_type="call")
    assert g["delta"] == 1.0
    assert g["rho"] != 0.0 or g["theta"] != 0.0
    # invalid S should raise not silent zeros
    import pytest
    with pytest.raises(ValueError):
        bs_greeks(S=-10, K=100, T=1, r=0.05, sigma=0.2)


def test_bs_price_invalid_numeric_chains():
    import pytest
    from hero_quant.quantlib.options import bs_price
    with pytest.raises(TypeError):
        bs_price(S="bad", K=100, T=1)


def test_implied_vol_invalid_and_cap():
    import pytest
    from hero_quant.quantlib.options import implied_volatility, bs_price
    # S<=0 should raise
    with pytest.raises(ValueError):
        implied_volatility(price=5, S=0, K=100, T=1)
    # price below intrinsic should raise
    with pytest.raises(ValueError):
        implied_volatility(price=1, S=120, K=100, T=1, r=0.05, option_type="call")
    # valid high vol should not silently truncate without signaling? Ensure within cap
    p_high = bs_price(S=100, K=100, T=1, r=0.05, sigma=5.0)
    iv = implied_volatility(price=p_high, S=100, K=100, T=1, r=0.05)
    assert abs(iv - 5.0) < 0.1


def test_scipy_fallback_narrow():
    # ensure norm functions are defined and not masking ImportError incorrectly
    from hero_quant.quantlib.options import _norm_cdf, _norm_pdf
    assert abs(_norm_cdf(0) - 0.5) < 1e-9
    assert _norm_pdf(0) > 0

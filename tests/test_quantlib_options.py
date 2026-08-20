# tests/test_quantlib_options.py — TDD Task 7 options pricing
def test_bs_price_at_expiry_is_intrinsic():
    from hero_quant.quantlib.options import bs_price

    assert bs_price(S=100, K=100, T=0) == 0

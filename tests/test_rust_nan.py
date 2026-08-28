import math
import pandas as pd
import types

from hero_quant.quantlib import indicators as py_ind
from hero_quant.quantlib import rust as rust_mod


def _fake_rust_module():
    """Fake Rust extension that mimics buggy 0.0 coercion vs correct behavior."""
    m = types.SimpleNamespace()

    def sma(data, window):
        # naive mean; if data contains 0.0 where original NaN was, will produce non-NaN
        # simulate Rust that does not know NaN => treats 0 as value
        out = []
        for i in range(len(data)):
            if i + 1 < window:
                out.append(None)
            else:
                window_vals = data[i + 1 - window : i + 1]
                # if any 0 placeholder from NaN, average will be wrong vs NaN
                # we compute mean directly; if caller incorrectly passed 0, we return value
                # if caller correctly avoided Rust path, this won't be called for NaN input
                out.append(sum(window_vals) / window)
        return out

    def ema(data, span):
        # simple passthrough for test: return data as is to detect NaN handling
        return list(data)

    def rsi(data, period):
        return [50.0] * len(data)

    def bollinger(data, window, num_std):
        n = len(data)
        mid = [None if i + 1 < window else sum(data[i + 1 - window : i + 1]) / window for i in range(n)]
        up = [None if v is None else v + 1 for v in mid]
        low = [None if v is None else v - 1 for v in mid]
        return mid, up, low

    def macd(data, fast, slow, signal):
        return (list(data), list(data), list(data))

    m.sma = sma
    m.ema = ema
    m.rsi = rsi
    m.bollinger = bollinger
    m.macd = macd
    return m


def test_sma_nan_preserved(monkeypatch):
    data = [1.0, float("nan"), 3.0, 4.0]
    py_res = py_ind.sma(data, 2)
    # monkeypatch Rust to be available and faulty if NaN coerced to 0
    fake = _fake_rust_module()
    monkeypatch.setattr(rust_mod, "_RUST_MOD", fake)
    monkeypatch.setattr(rust_mod, "IS_RUST", True)

    rust_res = rust_mod.sma(data, 2)
    # With correct fix, Rust path should be SKIPPED for NaN input and fallback to python -> must match py_res (NaNs preserved)
    # With buggy code, Rust path would be taken and 0.0 would corrupt result
    assert math.isnan(rust_res.iloc[1]), f"expected NaN at 1 got {rust_res.iloc[1]} - NaN was coerced to 0"
    assert math.isnan(rust_res.iloc[2]), f"expected NaN at 2 got {rust_res.iloc[2]}"
    for i in range(len(py_res)):
        pv = py_res.iloc[i]
        rv = rust_res.iloc[i]
        if pd.isna(pv):
            assert pd.isna(rv), f"mismatch at {i}: py NaN vs rust {rv}"
        else:
            assert rv == pv, f"mismatch at {i}: {pv} vs {rv}"
    assert rust_res.iloc[3] == 3.5


def test_clean_input_matches_python_within_tolerance(monkeypatch):
    data = [1.0, 2.0, 3.0, 4.0, 5.0]
    # For clean data, Rust path SHOULD be used and result must match python within 1e-9
    # Use real python fallback as expected; fake rust returns same as python for some but we need to test passthrough
    # Create fake that returns python-correct values (so we can verify Rust was called)
    called = {}

    orig_fake = _fake_rust_module()

    def tracking_sma(d, w):
        called["sma"] = True
        # compute correct sma using python logic but via fake
        s = pd.Series(d)
        return [None if i + 1 < w else float(s[i + 1 - w : i + 1].mean()) for i in range(len(d))]

    orig_fake.sma = tracking_sma
    monkeypatch.setattr(rust_mod, "_RUST_MOD", orig_fake)
    monkeypatch.setattr(rust_mod, "IS_RUST", True)

    py_res = py_ind.sma(data, 2)
    rust_res = rust_mod.sma(data, 2)
    assert called.get("sma") is True, "clean input should still use Rust path"
    for j in range(len(py_res)):
        pv = py_res.iloc[j]
        rv = rust_res.iloc[j]
        if pd.isna(pv) and pd.isna(rv):
            continue
        assert abs(float(pv) - float(rv)) < 1e-9


def test_prepare_helper_centralized():
    import inspect

    src = inspect.getsource(rust_mod)
    assert "_prepare_data" in src, "helper _prepare_data not found"
    assert "x==x" not in src, "fragile x==x still present"
    # ensure all 5 paths use helper
    count = src.count("_prepare_data")
    # helper def + 5 usages = at least 6
    assert count >= 6, f"_prepare_data should be used by all 5 paths, found {count} occurrences"

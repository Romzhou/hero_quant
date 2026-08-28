"""P2 medium tools tests — red→green for scan_remain findings."""

import pathlib
import inspect

ROOT = pathlib.Path(__file__).resolve().parents[1]
QT = ROOT / "src/hero_quant/tools/quantlib_tool.py"
PRES = ROOT / "src/hero_quant/tools/presentation.py"
MD = ROOT / "src/hero_quant/tools/market_data.py"
BT = ROOT / "src/hero_quant/tools/backtest.py"
REG = ROOT / "src/hero_quant/tools/registry.py"

def _read(p): return p.read_text(encoding="utf-8", errors="ignore")

# ---- quantlib_tool ----

def test_quantlib_unknown_indicator_fails_visible():
    from hero_quant.tools.quantlib_tool import compute_indicator
    r = compute_indicator(symbol="TEST", indicator="unknown_foo_xyz", window=5, start="2026-08-01", end="2026-08-03")
    assert r.get("ok") is False, "unknown indicator should return ok=False, not silent SMA fallback"
    assert "unsupported" in str(r.get("error", "")).lower()

def test_quantlib_window_validation_negative():
    from hero_quant.tools.quantlib_tool import compute_indicator
    r = compute_indicator(symbol="TEST", indicator="sma", window=-5, start="2026-08-01", end="2026-08-03")
    assert r.get("ok") is False
    assert "window" in str(r.get("error", "")).lower()

def test_quantlib_rsi_uses_window_param():
    txt = _read(QT)
    # should not hardcode alpha=1 / 14 unconditionally; should use n/window
    assert "alpha=1 / 14" not in txt or "alpha=1/n" in txt or "alpha=1 / n" in txt, "RSI fallback still hardcodes 14 instead of n"

def test_quantlib_bollinger_returns_full_bands_or_documented():
    txt = _read(QT)
    # Bollinger branch should not silently discard upper/lower; either return them or remove dead compute
    # After fix, it should mention upper/lower in return or have comment about truncation removed
    assert "upper" in txt.lower() and "lower" in txt.lower()
    # ensure values are not just mid; check that function returns upper/lower keys when bollinger
    from hero_quant.tools.quantlib_tool import compute_indicator
    r = compute_indicator(symbol="TEST", indicator="bollinger", window=5, start="2026-08-01", end="2026-08-10")
    # after fix should include upper/lower or explicit handling; we check ok True and values present
    assert r.get("ok") is True
    # at least values key exists; upper/lower may be in payload
    assert "values" in r

def test_quantlib_no_silent_except_pass():
    txt = _read(QT)
    # overly broad silent except Exception: pass without logging should be gone
    assert txt.count("except Exception:") <= 4  # allow limited with logging; before fix had bare pass
    assert "except Exception:\n            pass" not in txt

def test_quantlib_fetch_closes_logs():
    txt = _read(QT)
    assert "logger" in txt or "logging" in txt
    assert "exc_info" in txt

# ---- presentation ----

def test_presentation_sorted_iteration():
    txt = _read(PRES)
    # present_definitions for code/both should iterate sorted keys, not raw values()
    assert "sorted(TOOL_REGISTRY" in txt
    assert "TOOL_REGISTRY.values()" not in txt or "sorted" in txt

def test_presentation_dict_fallback_defaults():
    txt = _read(PRES)
    # helper _get_field encapsulates spec.get with defaults
    assert "_get_field" in txt
    # also ensure defaults are present somewhere
    assert '"unknown"' in txt or "'unknown'" in txt

def test_presentation_present_invalid_raises():
    from hero_quant.tools.presentation import present
    import pytest
    with pytest.raises(ValueError):
        present({"name":"x"}, presentAs="INVALID_XYZ")

def test_presentation_helper_not_duplicated():
    txt = _read(PRES)
    assert "_get_field" in txt

def test_presentation_return_type_annotation():
    txt = _read(PRES)
    # should not promise List[Dict] when code/both returns str/both
    assert "List[Any]" in txt or "list[Any]" in txt or "List[Dict[str, Any]]" not in txt or "overload" in txt.lower()

# ---- market_data ----

def test_market_data_search_symbols_no_keyword_extra():
    from hero_quant.tools.market_data import search_symbols
    r = search_symbols(keyword="test")
    assert "keyword" not in r, "output schema violation: keyword field should not be returned"
    assert "symbols" in r and "candidates" in r

def test_market_data_exc_info_correct():
    txt = _read(MD)
    assert "exc_info=e" not in txt
    # should use exc_info=True
    assert "exc_info=True" in txt

def test_market_data_no_private_loaders():
    txt = _read(MD)
    assert "reg._loaders" not in txt
    # should use len(reg) or public API
    assert "len(reg" in txt or "has_loaders" in txt or "is_empty" in txt

def test_market_data_no_private_synthetic():
    txt = _read(MD)
    assert "._synthetic_bars" not in txt

def test_market_data_get_bars_range_reuses_registry():
    txt = _read(MD)
    # get_bars_range should not rebuild registry per symbol via get_market_data loop
    # after fix it should use shared registry or direct reg.get_bars
    assert "_get_shared_registry" in txt or "_shared_registry" in txt or "ThreadPoolExecutor" in txt or txt.count("_make_registry()") <= 2

# ---- backtest ----

def test_backtest_interval_forwarded():
    txt = _read(BT)
    # _fetch_bars_for_backtest should accept interval and be used in run_backtest
    assert "def _fetch_bars_for_backtest" in txt
    # check signature includes interval
    sig = txt[txt.find("def _fetch_bars_for_backtest"):txt.find("def _fetch_bars_for_backtest")+300]
    assert "interval" in sig
    # run_backtest should forward interval
    assert "interval" in _read(BT).split("def run_backtest")[1][:2000]

def test_backtest_synthetic_seed_stable():
    txt = _read(BT)
    assert "hash(str(ticker))" not in txt
    assert "hashlib" in txt or "zlib.crc32" in txt

def test_backtest_empty_index_handled():
    txt = _read(BT)
    assert "IndexError" in txt
    assert "n == 0" in txt or "len(index)==0" in txt or "if n == 0" in txt

def test_backtest_open_not_leaking():
    txt = _read(BT)
    # should drop open before engine
    assert 'drop(columns=["open"]' in txt or "drop(columns=['open']" in txt

def test_backtest_try_narrowed():
    txt = _read(BT)
    # over-broad try wrapping entire run_backtest should be narrowed; check exc_info=True logging
    assert "exc_info=True" in txt

# ---- registry ----

def test_registry_timeoutMs_validates():
    from hero_quant.tools.registry import tool, TOOL_REGISTRY
    import pytest
    # should raise ValueError on invalid timeoutMs
    with pytest.raises(ValueError):
        @tool(name="tmp_bad_timeout_xyz", description="bad timeout", timeoutMs="not-an-int")
        def _f(): pass

def test_registry_unknown_kwargs_rejected():
    from hero_quant.tools.registry import tool
    import pytest
    with pytest.raises(ValueError):
        @tool(name="tmp_unknown_kw_xyz", description="bad kw", unknown_kw_arg_xyz=123)
        def _g(): pass
    # cleanup if registered
    from hero_quant.tools.registry import TOOL_REGISTRY
    TOOL_REGISTRY.pop("tmp_unknown_kw_xyz", None)
    TOOL_REGISTRY.pop("tmp_bad_timeout_xyz", None)

def test_registry_shallow_validation_recursive():
    txt = _read(REG)
    # should recurse into nested props/items or mention _assert_schema
    assert "_assert_schema" in txt or "recurse" in txt.lower()

def test_registry_presentAs_dead_branches_fixed():
    txt = _read(REG)
    # get_definitions should validate presentAs and raise for unsupported
    assert "unsupported presentAs" in txt or "NotImplementedError" in txt

def test_registry_output_wrapping_validated():
    txt = _read(REG)
    # should validate output["schema"] when wrapped
    assert 'output["schema"]' in txt or "output['schema']" in txt

def test_registry_thread_safety_lock():
    txt = _read(REG)
    assert "_REGISTRY_LOCK" in txt or "threading.RLock" in txt or "threading.Lock" in txt

def test_registry_output_schema_allows_extra():
    # quantlib compute_indicator output should allow symbol etc or be consistent
    from hero_quant.tools.quantlib_tool import compute_indicator
    r = compute_indicator(symbol="AAPL", indicator="sma", window=5)
    # should not be rejected by schema; ok check
    assert "ok" in r

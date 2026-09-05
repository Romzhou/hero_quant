"""Lane D2 retest-271 repro tests: 33 backtest-compute findings.

TDD file — each test fails on the pre-fix code and passes after the
prescribed OCR fix. Covers:
  bench.py (3), engine.py (6), metrics.py (4), validation.py (4),
  tools/backtest.py (4), tools/correlation.py (4),
  tools/market_data.py (3), tools/quantlib_tool.py (5).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def _prices(n=5, start=100.0, step=1.0):
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame({"close": [start + i * step for i in range(n)]}, index=idx)


# ============================================================ bench.py (3)
def test_d2_bench_output_symlink_escape_blocked(tmp_path, monkeypatch):
    """HIGH: multi-component relative path via symlink dir must be contained."""
    import hero_quant.backtest.bench as bench_mod

    outside = tmp_path / "outside"
    outside.mkdir()
    linkdir = tmp_path / "linkdir"
    try:
        linkdir.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="traversal|within|escapes"):
        bench_mod.run_batch(
            ["AAA"], dates=["2024-01-01", "2024-01-02"],
            output_dir="linkdir/evil.json", allow_synthetic=True,
        )


def test_d2_bench_bad_metric_does_not_abort_batch(monkeypatch):
    """MEDIUM: None/invalid cumulative_return must not abort the whole batch."""
    import hero_quant.backtest.bench as bench_mod
    from hero_quant.backtest.engine import BacktestEngine

    def _fake(self, prices, **k):
        return {"metrics": {"cumulative_return": None, "sharpe": 1.0}}

    monkeypatch.setattr(BacktestEngine, "run", _fake)
    res = bench_mod.run_batch(["A", "B"], dates=["2024-01-01", "2024-01-02"], allow_synthetic=True)
    assert set(res) == {"A", "B"}
    assert res["A"].get("alpha") is None or res["A"].get("failed")


def test_d2_bench_no_dead_allow_synthetic_guard():
    """LOW: in-loop duplicate allow_synthetic guard must be removed."""
    import pathlib

    src = pathlib.Path("src/hero_quant/backtest/bench.py").read_text(encoding="utf-8")
    # entry check already raises; in-loop duplicate must be gone, engine kwargs direct
    assert src.count("requires allow_synthetic=True") <= 1
    # error message must not promise a real-price path this harness has no param for
    assert "provide real price data" not in src


# ============================================================ engine.py (6)
def test_d2_engine_aligned_no_double_delay():
    """HIGH: aligned return must be applied at bar i, not buffered to i+1.

    With a +10%/bar ladder, bar-1 equity must already reflect the [0,1]
    executable move (1010), not the pending-delayed value.
    """
    import inspect

    import pandas as pd

    from hero_quant.backtest.engine import BacktestEngine

    run_src = inspect.getsource(BacktestEngine.run)
    assert "pending_aligned" not in run_src, "double-delay buffer still present"
    prices = pd.DataFrame(
        {"close": [100.0, 110.0, 121.0, 133.1, 146.41]},
        index=pd.date_range("2026-08-01", periods=5),
    )
    eng = BacktestEngine(initial_capital=1000.0)
    res = eng.run(prices, weights=[1.0], costs=0.0, allow_synthetic=True)
    eq = res["equity"]
    assert float(eq.iloc[1]) == pytest.approx(1100.0, rel=1e-6)
    # last bar has no next-day executable price: cur-bar fallback, no extra bar
    assert float(eq.iloc[-1]) == pytest.approx(1331.0, rel=1e-6)


def test_d2_engine_rejects_nonpositive_single_and_multi():
    """HIGH: single-asset and multi-asset paths must reject <=0 prices."""
    from hero_quant.backtest.engine import BacktestEngine, DataFeedError

    eng = BacktestEngine()
    bad_single = _prices(n=3).assign(close=[100.0, 0.0, 102.0])
    with pytest.raises(DataFeedError):
        eng._price_matrix(bad_single)
    idx = pd.date_range("2024-01-01", periods=3, freq="B")
    bad_multi = pd.DataFrame({"A": [100.0, 101.0, 102.0], "B": [50.0, -1.0, 52.0]}, index=idx)
    with pytest.raises(DataFeedError):
        eng._price_matrix(bad_multi)


def test_d2_engine_market_neutral_not_zeroed():
    """HIGH: w=[0.5,-0.5] must keep gross exposure (not zero daily_ret/positions)."""
    from hero_quant.backtest.engine import BacktestEngine

    idx = pd.date_range("2024-01-01", periods=5, freq="B")
    prices = pd.DataFrame(
        {"A": [100, 101, 102, 103, 104], "B": [50, 51, 52, 53, 54]}, index=idx
    )
    eng = BacktestEngine(initial_capital=1000.0)
    res = eng.run(prices, weights=np.array([0.5, -0.5]), costs=0.0, allow_synthetic=True)
    pos = res["positions"]
    assert float(pos.abs().sum(axis=1).iloc[-1]) > 0, "gross exposure zeroed for market-neutral book"


def test_d2_engine_turnover_uses_aligned_levered_path():
    """MEDIUM: turnover proxy must include leverage factor of the main loop."""
    import inspect

    from hero_quant.backtest.engine import BacktestEngine

    run_src = inspect.getsource(BacktestEngine.run)
    # single-asset and multi-asset pos_proxy branches must scale by leverage
    assert run_src.count("* _lev") >= 2, "pos_proxy branches miss leverage scaling"


def test_d2_engine_malformed_price_contract_documented():
    """MEDIUM: _price_matrix DataFeedError must be caught/documented at run entry."""
    import inspect

    from hero_quant.backtest.engine import BacktestEngine

    src = inspect.getsource(BacktestEngine.run)
    assert "DataFeedError" in src


def test_d2_engine_capital_check_not_tautological():
    """MEDIUM: capital pre-check must compare against an explicit limit, not eq*lev."""
    import inspect

    from hero_quant.backtest.engine import BacktestEngine

    src = inspect.getsource(BacktestEngine.run)
    assert "available_capital=eq * _lev" not in src


# ============================================================ metrics.py (4)
def test_d2_metrics_dataframe_branch_reachable():
    """HIGH: max_drawdown/annual_return must accept single-column DataFrame."""
    from hero_quant.backtest.metrics import annual_return, max_drawdown

    idx = pd.date_range("2024-01-01", periods=5, freq="B")
    df = pd.DataFrame({"equity": [100.0, 101.0, 99.0, 102.0, 103.0]}, index=idx)
    assert np.isfinite(max_drawdown(df))
    assert np.isfinite(annual_return(df))


def test_d2_metrics_negative_ratio_no_complex_crash():
    """HIGH: negative end/start ratio must return 0.0, not raise TypeError."""
    from hero_quant.backtest.metrics import annual_return

    s = pd.Series([5.0, 3.0, 1.0, -2.0])
    assert annual_return(s) == 0.0


def test_d2_metrics_costs_cannot_invert_equity():
    """MEDIUM: excessive costs bankrupt to floored 0, never negative/complex."""
    from hero_quant.backtest.metrics import compute_metrics

    idx = pd.date_range("2024-01-01", periods=5, freq="B")
    s = pd.Series([100.0, 101.0, 102.0, 103.0, 104.0], index=idx)
    m = compute_metrics(s, costs=5.0)
    # limited-liability floor: total loss cumret == -1.0, finite, no crash
    assert m["cumulative_return"] == pytest.approx(-1.0, abs=1e-9)
    assert np.isfinite(m["annual_return"]) and np.isfinite(m["max_drawdown"])


def test_d2_metrics_turnover_weights_single_side():
    """MEDIUM: positions and weights paths must share single-side /2 semantics."""
    from hero_quant.backtest.metrics import turnover

    assert turnover(None, weights=[0, 1, 0, 1, 0]) == pytest.approx(0.5, rel=0.01)
    assert turnover(pd.Series([0, 1, 0, 1, 0], dtype=float), None) == pytest.approx(0.5, rel=0.01)


# ============================================================ validation.py (4)
def test_d2_validation_extra_positionals_rejected():
    """HIGH: extra positionals are rejected, never remapped (indices misalign).

    Python binds the first positionals to named params, so leftover *args
    indices no longer correspond to slots — remapping drops/misaligns values.
    """
    from hero_quant.backtest.validation import ValidationError, validate

    df = _prices(n=3)
    with pytest.raises(ValidationError):
        validate(df, "2024-01-02", "2024-01-03", "USD", "EXTRA")
    # overlap of positional with explicit kwarg is rejected (TypeError from
    # binding, or ValidationError) — must never silently drop the positional
    with pytest.raises((ValidationError, TypeError)):
        validate(df, "2024-01-02", weights_on="2024-01-01")
    # misaligned remap case: bound None params + 2 extras must raise, not
    # silently validate against a dropped/misaligned date
    with pytest.raises(ValidationError):
        validate(df, "W", None, None, "P", "C")


def test_d2_validation_rejects_infinite_prices():
    """HIGH(rescan): +inf is neither NaN nor <=0 — must be rejected as dirty."""
    import numpy as _np

    from hero_quant.backtest.validation import ValidationError, validate

    idx = pd.date_range("2024-01-01", periods=3, freq="B")
    df = pd.DataFrame({"close": [100.0, _np.inf, 102.0]}, index=idx)
    with pytest.raises(ValidationError):
        validate(df)
    df2 = pd.DataFrame({"A": [100.0, 101.0, 102.0], "B": [50.0, _np.inf, 52.0]}, index=idx)
    with pytest.raises(ValidationError):
        validate(df2)


def test_d2_validation_currency_case_insensitive():
    """HIGH: Currency/CCY/ccy variants must enter the mixed-currency gate."""
    from hero_quant.backtest.validation import ValidationError, validate

    idx = pd.date_range("2024-01-01", periods=2, freq="B")
    df = pd.DataFrame({"close": [100.0, 101.0], "CCY": ["USD", "EUR"]}, index=idx)
    with pytest.raises(ValidationError):
        validate(df)


def test_d2_validation_open_validated():
    """HIGH: dirty open/high/low must be rejected (engine uses open for execution)."""
    from hero_quant.backtest.validation import ValidationError, validate

    idx = pd.date_range("2024-01-01", periods=3, freq="B")
    df = pd.DataFrame(
        {"close": [100.0, 101.0, 102.0], "open": [99.0, -5.0, 101.0]}, index=idx
    )
    with pytest.raises(ValidationError):
        validate(df)


def test_d2_validation_mixed_tz_rejected():
    """MEDIUM: genuinely ambiguous mixed naive/aware pairs must fail closed.

    OCR prescription (mixed awareness can invert verdict by hours) vs the
    Task13-10 contract (same instant in any representation is NOT a
    violation): normalize-then-compare keeps same-instant pairs valid, while
    a mixed pair with the same wall-clock reading and a NON-UTC aware offset
    is fail-closed rejected (assuming UTC for the naive side could flip the
    verdict by the offset hours).
    """
    from hero_quant.backtest.validation import ValidationError, validate

    df = _prices(n=2)
    # ambiguous: same wall clock, aware side is +05:00 -> verdict depends on
    # the naive-as-UTC assumption -> must fail closed
    with pytest.raises(ValidationError):
        validate(
            df,
            weights_on=pd.Timestamp("2024-01-01 09:00", tz="Etc/GMT-5"),
            price_date=pd.Timestamp("2024-01-01 09:00"),
        )
    # unambiguous: UTC-aware vs naive same instant stays valid (Task13-10)
    validate(
        df,
        weights_on=pd.Timestamp("2024-01-01", tz="UTC"),
        price_date=pd.Timestamp("2024-01-01"),
    )
    # unambiguous: genuine future-data use still caught after normalization
    with pytest.raises(ValidationError):
        validate(
            df,
            weights_on=pd.Timestamp("2024-01-03", tz="UTC"),
            price_date=pd.Timestamp("2024-01-02"),
        )


# ============================================================ tools/backtest.py (4)
def test_d2_tool_backtest_multi_asset_provenance_synthetic(monkeypatch):
    """HIGH: multi-asset synthetic price matrix must report synthetic provenance."""
    import hero_quant.tools.backtest as bt

    monkeypatch.setattr(bt, "_fetch_bars_for_backtest", lambda *a, **k: [])
    r = bt.run_backtest(symbol="AAPL,MSFT", weights=[0.5, 0.5])
    assert r.get("ok") is True
    assert (r.get("provenance") or {}).get("source") == "synthetic"


def test_d2_tool_backtest_validate_envelope():
    """HIGH: PIT violation must return {valid:False, ok:False}, not raise."""
    from hero_quant.tools.backtest import validate_backtest

    r = validate_backtest(weights_on="2026-08-10", price_date="2026-08-01")
    assert r == {"valid": False, "ok": False, "error": r["error"]}
    assert r["ok"] is False and r["valid"] is False


def test_d2_tool_backtest_empty_bars_honors_range(monkeypatch):
    """MEDIUM: empty-bars fallback must honor requested start/end length."""
    import hero_quant.tools.backtest as bt
    import hero_quant.backtest.engine as eng_mod

    seen = {}

    class _Stub:
        def run(self, prices, weights=None, costs=0.0005, engine="default", **kw):
            seen["n"] = len(prices)
            return {"equity": [100.0, 101.0], "metrics": {}}

    monkeypatch.setattr(bt, "_fetch_bars_for_backtest", lambda *a, **k: [])
    monkeypatch.setattr(eng_mod, "BacktestEngine", _Stub)
    r = bt.run_backtest(symbol="AAA", start="2026-01-01", end="2026-03-01", weights=[1.0])
    assert r.get("ok") is True
    assert seen["n"] > 3, f"date range collapsed to {seen['n']} points"


def test_d2_tool_backtest_empty_equity_envelope():
    """MEDIUM: empty/None equity must return ok:False, not ok:True zeros."""
    from hero_quant.tools.backtest import get_backtest_metrics

    assert get_backtest_metrics([]).get("ok") is False
    assert get_backtest_metrics(None).get("ok") is False


# ============================================================ tools/correlation.py (4)
def test_d2_corr_date_keys_normalized(monkeypatch):
    """HIGH: heterogeneous date reps for the same day must join."""
    import hero_quant.tools.correlation as corr

    base = [100 + i * 0.5 for i in range(10)]
    import datetime

    dates_a = ["2026-07-01", "2026-07-02", "2026-07-03 00:00:00", "2026-07-04", "2026-07-05",
               "2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10"]
    dates_b = [datetime.datetime(2026, 7, d, 15, 30) for d in range(1, 11)]

    def fake_fetch(symbol, start, end):
        if symbol == "A":
            return list(zip(dates_a, base))
        return list(zip(dates_b, [x + 0.05 for x in base]))

    monkeypatch.setattr(corr, "_fetch_closes", fake_fetch)
    r = corr.compute_correlation("A", "B", start="2026-07-01", end="2026-07-10")
    assert r.get("ok") is True, f"same days failed to join: {r}"
    assert r.get("points", 0) >= 2


def test_d2_corr_provenance_propagated(monkeypatch):
    """MEDIUM: success path must propagate get_bars provenance."""
    import types

    import hero_quant.tools.correlation as corr
    import hero_quant.data.registry as reg_mod

    base = [100 + i * 0.5 for i in range(10)]

    class _Reg:
        def register(self, loader):
            pass

        def get_bars(self, symbol, start, end, interval="1d"):
            bars = [{"date": f"2026-07-{i + 1:02d}", "close": base[i] + (0.1 if symbol == "B" else 0.0)} for i in range(10)]
            return bars, types.SimpleNamespace(source="tencent", unit="shares")

    monkeypatch.setattr(reg_mod, "MarketDataRegistry", _Reg)
    r = corr.compute_correlation("A", "B", start="2026-07-01", end="2026-07-10")
    assert r.get("ok") is True
    assert "provenance" in r or "isMock" in r


def test_d2_corr_synthetic_routing_explicit():
    """MEDIUM: synthetic routing must not depend on message substring."""
    import pathlib

    src = pathlib.Path("src/hero_quant/tools/correlation.py").read_text(encoding="utf-8")
    assert '"synthetic" in msg' not in src and "'synthetic' in msg" not in src
    assert '"synthetic" in str' not in src and "'synthetic' in str" not in src


def test_d2_corr_no_unreachable_valueerror():
    """LOW: dead ValueError branch / masked Settings cause must be fixed."""
    import inspect

    import hero_quant.tools.correlation as corr

    src = inspect.getsource(corr._fetch_closes)
    assert "except ValueError:\n            raise" not in src


# ============================================================ tools/market_data.py (3)
def test_d2_market_batch_fail_closed_on_validation(monkeypatch):
    """HIGH: batch must re-raise ValueError/TypeError per-symbol, not synthetic-fallback."""
    import types

    import hero_quant.tools.market_data as md

    def _boom(sym, *a, **k):
        raise ValueError("bad date format")

    monkeypatch.setattr(md, "_get_shared_registry", lambda: types.SimpleNamespace(get_bars=_boom))
    with pytest.raises(ValueError):
        md.get_bars_range(["AAA"], start="bad", end="also-bad")


def test_d2_market_no_loader_no_substring():
    """MEDIUM: no-loader detection must not use message substring."""
    import pathlib

    src = pathlib.Path("src/hero_quant/tools/market_data.py").read_text(encoding="utf-8")
    assert '"no loader" in str' not in src and "'no loader' in str" not in src


def test_d2_market_synth_fallback_reraises_validation(monkeypatch):
    """MEDIUM: synthetic fallback must re-raise ValueError/TypeError from the helper."""
    import hero_quant.tools.market_data as md

    with pytest.raises((ValueError, TypeError)):
        md._synthetic_fallback("AAA", start="not-a-date!!", end="also-bad!!")


# ============================================================ tools/quantlib_tool.py (5)
def test_d2_quantlib_macd_uses_window(monkeypatch):
    """HIGH: MACD must honor validated window n (not hardcoded 12/26/9)."""
    import hero_quant.tools.quantlib_tool as qt

    closes = [100 + i * 0.5 + (0.3 if i % 2 else -0.3) for i in range(60)]
    monkeypatch.setattr(qt, "_fetch_closes", lambda *a, **k: list(closes))
    r12 = qt.compute_indicator(symbol="T", indicator="macd", window=12,
                               start="2026-08-01", end="2026-10-01")
    r30 = qt.compute_indicator(symbol="T", indicator="macd", window=30,
                               start="2026-08-01", end="2026-10-01")
    assert r12.get("ok") is True and r30.get("ok") is True
    assert r12.get("values") != r30.get("values"), "window has no effect on MACD"


def test_d2_quantlib_rsi_flat_not_zero(monkeypatch):
    """HIGH: flat series RSI must not be 0 (extreme oversold) on any path."""
    import hero_quant.tools.quantlib_tool as qt

    monkeypatch.setattr(qt, "_fetch_closes", lambda *a, **k: [100.0] * 30)
    # force the pandas fallback by hiding quantlib callables (file already
    # imports the module, so patch its rsi attr to None)
    import hero_quant.quantlib.indicators as qi

    monkeypatch.setattr(qi, "rsi", None)
    r = qt.compute_indicator(symbol="T", indicator="rsi", window=14,
                             start="2026-08-01", end="2026-09-01")
    assert r.get("ok") is True
    vals = r.get("values") or []
    assert len(vals) == 30, f"expected 30 RSI slots, got {len(vals)}"
    # flat series: no slot may read 0.0 (extreme oversold); undefined slots
    # surface as None instead of the 1e-9-fudged 0.0
    assert all(v != 0.0 for v in vals if v is not None), f"flat RSI fudged to 0: {vals}"
    assert all(v is None for v in vals), f"flat series RSI should be undefined (None), got {vals}"


def test_d2_quantlib_failure_returns_none_not_zero(monkeypatch):
    """MEDIUM: Sharpe/drawdown failures must return None, not 0.0 sentinel."""
    from hero_quant.tools.quantlib_tool import compute_drawdown, compute_sharpe

    r = compute_sharpe([])
    assert r.get("ok") is False and r.get("sharpe") is None
    r2 = compute_drawdown([])
    assert r2.get("ok") is False and r2.get("drawdown") is None


def test_d2_quantlib_no_dead_ternary():
    """LOW: dead `n if n else 14` must be removed."""
    import pathlib

    src = pathlib.Path("src/hero_quant/tools/quantlib_tool.py").read_text(encoding="utf-8")
    assert "n if n else 14" not in src


def test_d2_quantlib_sharpe_reuses_coerced():
    """LOW: compute_sharpe must reuse single coerced Series (catches ['foo'])."""
    import pathlib

    from hero_quant.tools.quantlib_tool import compute_sharpe

    src = pathlib.Path("src/hero_quant/tools/quantlib_tool.py").read_text(encoding="utf-8")
    assert "_ = pd.to_numeric" not in src
    r = compute_sharpe(["foo", "bar"])
    assert r.get("ok") is False

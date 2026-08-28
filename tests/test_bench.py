"""TDD C1-1 bench batch + regional benchmark — RED before impl."""
import json
import pathlib
import tempfile


def test_run_batch_regional():
    from hero_quant.backtest.bench import run_batch, _resolve_benchmark

    # suffix map must mirror TradingAgents default_config.py:152
    assert _resolve_benchmark("600519.SS") == "000001.SS"
    assert _resolve_benchmark("0700.HK") == "^HSI"
    # US default
    assert _resolve_benchmark("AAPL") == "SPY"
    # explicit override
    assert _resolve_benchmark("600519.SS", benchmark_ticker="SPY") == "SPY"

    with tempfile.TemporaryDirectory() as tmp:
        dates = ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
        metrics = run_batch(["600519.SS", "0700.HK"], dates=dates, output_dir=tmp)

        # metrics dict per ticker
        assert "600519.SS" in metrics
        assert "0700.HK" in metrics
        # regional distinction
        assert metrics["600519.SS"]["benchmark"] == "000001.SS"
        assert metrics["0700.HK"]["benchmark"] == "^HSI"
        assert metrics["600519.SS"]["benchmark"] != metrics["0700.HK"]["benchmark"]
        # alpha present
        assert "alpha" in metrics["600519.SS"]
        assert "alpha" in metrics["0700.HK"]
        # alpha_vs label contains benchmark
        assert "000001.SS" in metrics["600519.SS"].get("alpha_vs", "") or "alpha" in metrics["600519.SS"].get("alpha_vs", "").lower()
        assert "^HSI" in metrics["0700.HK"].get("alpha_vs", "")
        # sharpe / cumulative from engine
        assert "sharpe" in metrics["600519.SS"]
        assert "cumulative_return" in metrics["600519.SS"]

        # metrics.json written
        p = pathlib.Path(tmp) / "metrics.json"
        assert p.exists(), "run_batch should write metrics.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["600519.SS"]["benchmark"] == "000001.SS"
        assert data["0700.HK"]["benchmark"] == "^HSI"

        # single-date shorthand also works
        metrics2 = run_batch(["600519.SS", "0700.HK"], dates=["2024-01-01"], output_dir=tmp)
        assert metrics2["600519.SS"]["benchmark"] == "000001.SS"


def test_synthetic_prices_deterministic_seed():
    """Task13-11: bench synthetic seed must be deterministic via hashlib.sha256, not hash()."""
    from hero_quant.backtest.bench import _synthetic_prices
    import pandas as pd
    idx = pd.date_range("2024-01-01", periods=5)
    df1 = _synthetic_prices(idx, "AAPL")
    df2 = _synthetic_prices(idx, "AAPL")
    assert df1["close"].equals(df2["close"]), "same ticker must give identical synthetic prices"
    df3 = _synthetic_prices(idx, "MSFT")
    assert not df1["close"].equals(df3["close"])


def test_run_batch_engine_exception_logged(caplog):
    """Task13-12a: bench run_batch must log engine exceptions with exc_info, not silently swallow."""
    from hero_quant.backtest.bench import run_batch
    import tempfile
    import logging
    from unittest.mock import patch
    from hero_quant.backtest import bench as bench_mod
    # Force engine.run to raise
    with patch.object(bench_mod.BacktestEngine, "run", side_effect=RuntimeError("engine boom")):
        caplog.set_level(logging.WARNING)
        with tempfile.TemporaryDirectory() as tmp:
            res = run_batch(["AAPL"], dates=["2024-01-01"], output_dir=tmp)
            # Should still produce fallback metrics
            assert "AAPL" in res
            assert res["AAPL"]["sharpe"] == 0.0
            # Should have logged warning with exc_info
            assert any("engine run failed" in rec.message for rec in caplog.records)


def test_run_batch_io_failure_surfaces(tmp_path):
    """Task13-12b: bench IO failures for metrics.json/tearsheet must surface (raise), not silent pass."""
    from hero_quant.backtest.bench import run_batch
    import pathlib
    import pytest
    # Use a file as output_dir to force failure (bench should raise)
    file_path = tmp_path / "blockfile"
    file_path.write_text("block")
    with pytest.raises(Exception):
        run_batch(["AAPL"], dates=["2024-01-01"], output_dir=file_path)

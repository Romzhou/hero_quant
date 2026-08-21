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

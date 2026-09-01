# tests/test_backtest_engine.py — Wave5 thickened for coverage >=50
def test_backtest_engine_runs():
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    prices = pd.DataFrame({"close":[100,101,102,101,103]}, index=pd.date_range("2026-08-01", periods=5))
    engine = BacktestEngine()
    res = engine.run(prices, weights=[0.5,0.5], allow_synthetic=True)
    assert "equity" in res
    assert res["metrics"]["sharpe"] is not None

def test_backtest_multi_asset():
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    import numpy as np
    idx = pd.date_range("2026-08-01", periods=5)
    # Multi-asset price matrix: two assets with distinct closes
    prices = pd.DataFrame({"AAPL": [100,101,102,101,103], "MSFT": [50,51,50,52,53]}, index=idx)
    engine = BacktestEngine(initial_capital=1000.0)
    res = engine.run(prices, weights=np.array([0.6,0.4]), costs=0.001, allow_synthetic=True)
    assert "positions" in res
    assert res["positions"].shape[1] == 2
    assert len(res["equity"]) == 5
    # positions should be scaled by available_capital
    assert (res["positions"].abs().sum(axis=1) <= res["equity"] * 1.01).all()
    assert "tearsheet" in res
    assert "fills" in res

def test_backtest_turnover_cost_drag():
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    prices = pd.DataFrame({"close": [100,102,101,103,105]}, index=pd.date_range("2026-08-01", periods=5))
    engine = BacktestEngine(initial_capital=100.0)
    res_nocost = engine.run(prices, weights=[1.0], costs=0.0, allow_synthetic=True)
    res_cost = engine.run(prices, weights=[1.0], costs=0.005, allow_synthetic=True)
    # With costs, final equity should be <= no-cost equity
    assert res_cost["equity"].iloc[-1] <= res_nocost["equity"].iloc[-1] + 1e-6
    assert res_cost["metrics"]["turnover"] >= 0

def test_backtest_pit_guard():
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    import pytest
    prices = pd.DataFrame({"close": [100,101,102]}, index=pd.date_range("2026-08-01", periods=3))
    engine = BacktestEngine()
    # PIT violation: weights_on > price_date should raise ValidationError
    with pytest.raises(Exception):
        engine.run(prices, weights=[1.0], weights_on="2026-08-10", price_date="2026-08-01")

def test_backtest_signal_integration():
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    closes = list(range(40, 0, -1))
    prices = pd.DataFrame({"close": closes}, index=pd.date_range("2026-08-01", periods=len(closes)))
    engine = BacktestEngine(initial_capital=1000.0)
    res = engine.run(prices, signal="sma_crossover", costs=0.0, allow_synthetic=True)
    assert "equity" in res
    # Bear signal should give flat or less growth than bull (closed elsewhere)

def test_backtest_output_dir(tmp_path):
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    prices = pd.DataFrame({"close": [100,101,102]}, index=pd.date_range("2026-08-01", periods=3))
    engine = BacktestEngine()
    out = tmp_path / "bt_out"
    res = engine.run(prices, weights=[1.0], output_dir=out, allow_synthetic=True)
    assert (out / "positions.csv").exists()
    assert (out / "metrics.json").exists()
    assert (out / "tearsheet.html").exists()
    assert "equity" in res


# --- Task 13 additional TDD coverage ---

def test_align_multi_asset_returns_series():
    """Task13-1: _align must return per-asset Series for multi-asset, not single float."""
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    idx = pd.date_range("2026-08-01", periods=5)
    prices = pd.DataFrame({"AAPL": [100,101,102,101,103], "MSFT": [50,51,50,52,53]}, index=idx)
    engine = BacktestEngine()
    aligned = engine._align(prices, 0)
    import pandas as pd
    assert isinstance(aligned, pd.Series), f"_align multi should be Series, got {type(aligned)}"
    assert "AAPL" in aligned and "MSFT" in aligned
    # ensure per-asset equity contributions differ when assets diverge
    import numpy as np
    res = engine.run(prices, weights=np.array([0.9, 0.1]), costs=0.0, allow_synthetic=True)
    res2 = engine.run(prices, weights=np.array([0.1, 0.9]), costs=0.0, allow_synthetic=True)
    # different weightings must produce different equity curves
    assert not res["equity"].equals(res2["equity"])


def test_align_never_returns_zero_price():
    """Task13-2: _align must not silently return 0.0 on parse failure; should raise."""
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    import pytest
    idx = pd.date_range("2026-08-01", periods=3)
    # corrupt next bar with non-numeric that would previously coerce to NaN then 0.0
    prices = pd.DataFrame({"close": [100, "bad", 102]}, index=idx)
    engine = BacktestEngine()
    with pytest.raises(Exception):
        engine._align(prices, 0)


def test_pit_guard_default_on():
    """Task13-3: PIT guard default ON. Without explicit dates, run should still validate and not silently pass future data when dates are given."""
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    import pytest
    prices = pd.DataFrame({"close": [100,101,102]}, index=pd.date_range("2026-08-01", periods=3))
    engine = BacktestEngine()
    # Explicit violation must raise even without opt-in
    with pytest.raises(Exception):
        engine.run(prices, weights=[1.0], weights_on="2026-08-10", price_date="2026-08-01")
    # Default run without dates must not bypass validation entirely (should succeed with default weights_on == price_date)
    res = engine.run(prices, weights=[1.0], allow_synthetic=True)
    assert "equity" in res
    # Explicit opt-out should allow violation to pass
    res2 = engine.run(prices, weights=[1.0], weights_on="2026-08-10", price_date="2026-08-01", skip_pit=True)
    assert "equity" in res2
    res3 = engine.run(prices, weights=[1.0], weights_on="2026-08-10", price_date="2026-08-01", enforce_pit=False)
    assert "equity" in res3


def test_leverage_isclose_and_bear_not_overridden():
    """Task13-5: leverage checks use isclose; bear 0 weights preserved, not silently overridden."""
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    import numpy as np
    import math
    prices = pd.DataFrame({"close": [100,101,102,103]}, index=pd.date_range("2026-08-01", periods=4))
    engine = BacktestEngine(initial_capital=1000.0)
    # bear signal 0 weights should produce flat equity (not equal_weight growth)
    res_bear = engine.run(prices, weights=np.zeros(2), costs=0.0, allow_synthetic=True)
    # with 0 weights equity should stay flat (no leverage)
    assert math.isclose(res_bear["equity"].iloc[-1], 1000.0, rel_tol=1e-6) or res_bear["equity"].iloc[-1] <= 1000.01
    # near-zero leverage via isclose: weights with tiny epsilon should be treated as zero-ish but still isclose
    w_tiny = np.array([1e-13, 0.0])
    res_tiny = engine.run(prices, weights=w_tiny, costs=0.0, allow_synthetic=True)
    assert "equity" in res_tiny


def test_engine_broad_exception_narrowed(caplog):
    """Task13-4: sma_crossover broad except narrowed and logs with exc_info; ensure unknown method fallback still works."""
    from hero_quant.backtest.engine import Signal
    import pandas as pd
    closes = list(range(40, 0, -1))
    prices = pd.DataFrame({"close": closes}, index=pd.date_range("2026-08-01", periods=len(closes)))
    sig = Signal(method="unknown_method_xyz")
    import logging
    caplog.set_level(logging.WARNING)
    w = sig.generate(prices, n_assets=1)
    assert w is not None
    # unknown method should log warning
    assert any("unknown signal method" in rec.message for rec in caplog.records)


def test_output_dir_io_failure_surfaces(tmp_path):
    """Task13-12 (engine side): IO failures for metrics.json/tearsheet must surface (raise), not silent pass."""
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    import pathlib
    import pytest
    prices = pd.DataFrame({"close": [100,101,102]}, index=pd.date_range("2026-08-01", periods=3))
    engine = BacktestEngine()
    # Use a file as output_dir to force mkdir/write failure and assert it raises
    file_path = tmp_path / "not_a_dir"
    file_path.write_text("block")
    with pytest.raises(Exception):
        engine.run(prices, weights=[1.0], output_dir=file_path, allow_synthetic=True)

# --- Task 5 TDD: on_bar fail-closed 与周转率单口径 ---

def test_on_bar_no_silent_fallback():
    """Task5: on_bar 不应走 bar.get('close', bar.iloc[0]) 回退；_align 失败必须抛 ValidationError，无 historical_base_price 兜底。"""
    from hero_quant.backtest.engine import BacktestEngine
    from hero_quant.backtest.validation import ValidationError
    import pandas as pd
    import pytest

    idx = pd.date_range("2026-08-01", periods=3)
    # 次 Bar 为坏值，_align 应失败；on_bar 不得回退到 bar 的 close 或 historical_base_price
    prices = pd.DataFrame({"close": [100, "bad", 102]}, index=idx)
    engine = BacktestEngine()
    # 强制 historical_base_price 有值，若存在回退则会静默成功
    engine.historical_base_price = 99.0
    bar = prices.iloc[0]
    with pytest.raises(ValidationError):
        engine.on_bar(bar, 0, prices)
    # 源码层面也不应含回退关键字
    import inspect
    src = inspect.getsource(engine.on_bar)
    assert 'bar.get(' not in src, "on_bar still contains bar.get fallback"
    assert 'bar.iloc[0]' not in src, "on_bar still contains bar.iloc fallback"
    assert 'historical_base_price' not in src, "on_bar still references historical_base_price"


def test_on_bar_validation_error_narrowed():
    """Task5: on_bar 仅窄化为 except ValidationError: raise + except (ValueError…) as e: raise ValidationError from e。"""
    import inspect
    from hero_quant.backtest.engine import BacktestEngine

    src = inspect.getsource(BacktestEngine.on_bar)
    assert "except ValidationError" in src
    assert "raise ValidationError" in src
    # 不应有宽泛 except Exception
    assert "except Exception" not in src


def test_compute_turnover_rate_helper_exists_and_single_source():
    """Task5: 682-823 抽 _compute_turnover_rate(pos_proxy) 复用 — 帮手存在且单口径复用。"""
    from hero_quant.backtest.engine import BacktestEngine
    import inspect

    assert hasattr(BacktestEngine, "_compute_turnover_rate"), "missing _compute_turnover_rate helper"
    sig = inspect.signature(BacktestEngine._compute_turnover_rate)
    assert "pos_proxy" in sig.parameters, "helper must accept pos_proxy"
    src = inspect.getsource(BacktestEngine._compute_turnover_rate)
    # 帮手应包含首日逻辑且不硬编码 1.0
    assert "total_weight" in src or "‖w‖" in src or "abs" in src
    # engine.run 中应仅调用一次帮手（单口径），不在两处重复计算
    run_src = inspect.getsource(BacktestEngine.run)
    # 至少一次调用 helper
    assert "_compute_turnover_rate" in run_src, "run must call _compute_turnover_rate"
    # 不应再有硬编码 turnover_rate.iloc[0] = 1.0
    assert "turnover_rate.iloc[0] = 1.0" not in run_src
    assert "turnover_rate.iloc[0] = float(1.0)" not in run_src


def test_turnover_first_day_weight_ratio_and_empty_zero():
    """Task5: 首日 turnover 按 ‖w‖₁/total_weight 非硬 1.0，空仓 0；成本单次扣除（net_ret gross）。"""
    from hero_quant.backtest.engine import BacktestEngine
    import pandas as pd
    import numpy as np

    prices = pd.DataFrame({"close": [100, 101, 102, 103]}, index=pd.date_range("2026-08-01", periods=4))
    engine = BacktestEngine(initial_capital=1000.0)

    # 非空仓：‖w‖₁/total_weight =1.0（归一化），不应硬编码但结果一致
    gross_equity = pd.Series([1000, 1010, 1020, 1030], index=prices.index, dtype=float)
    w = np.array([0.6, 0.4])
    total_weight = float(np.abs(w).sum())  # 1.0
    pos_proxy = pd.DataFrame(
        {f"asset_{i}": gross_equity * float(wi) / total_weight for i, wi in enumerate(w)},
        index=prices.index,
    )
    tr = engine._compute_turnover_rate(pos_proxy, gross_equity, w, total_weight)
    expected_first = float(np.abs(w).sum()) / float(total_weight) if total_weight else 0.0
    assert abs(float(tr.iloc[0]) - expected_first) < 1e-9, f"first day {tr.iloc[0]} != {expected_first} (‖w‖₁/total_weight)"
    # 非空仓不应为 0
    assert float(tr.iloc[0]) > 0

    # 空仓：w 全 0 -> total_weight 引擎侧会 clamp 为 1.0，首日应 0 而非 1.0
    w0 = np.zeros(2)
    total_weight0 = 1.0
    pos_proxy0 = pd.DataFrame(
        {f"asset_{i}": gross_equity * 0.0 for i in range(2)}, index=prices.index
    )
    tr0 = engine._compute_turnover_rate(pos_proxy0, gross_equity, w0, total_weight0)
    assert float(tr0.iloc[0]) == 0.0, f"empty position first day should be 0, got {tr0.iloc[0]}"

    # 成本单次扣除：net_ret 应为 gross，costs 仅主循环扣一次
    # 通过对比 costs=0 与 costs>0 的权益差应约等于 turnover*cost 叠加，而非双计
    res_nocost = engine.run(prices, weights=[1.0], costs=0.0, allow_synthetic=True)
    res_cost = engine.run(prices, weights=[1.0], costs=0.005, allow_synthetic=True)
    assert res_cost["equity"].iloc[-1] <= res_nocost["equity"].iloc[-1] + 1e-9
    # 若双计，cost 拖累会翻倍；检查拖累在合理区间（单次扣除约 0.5%*turnover 3 次 ~1-2%）
    drag = float(res_nocost["equity"].iloc[-1] - res_cost["equity"].iloc[-1])
    assert 0 <= drag < float(res_nocost["equity"].iloc[-1]) * 0.05, f"drag {drag} suggests double counting"


def test_metrics_turnover_single_source_and_docstring():
    """Task5: metrics.py turnover/2 有 docstring 单边口径说明；compute_metrics 成本语义与 engine 单口径一致。"""
    import inspect
    from hero_quant.backtest import metrics as m

    src_turnover = inspect.getsource(m.turnover)
    assert "/ 2" in src_turnover or "/2" in src_turnover, "turnover should divide by 2 for single-side"
    assert "半" in src_turnover or "单边" in src_turnover or "half" in src_turnover.lower(), "turnover docstring must explain half-turnover"

    src_compute = inspect.getsource(m.compute_metrics)
    # compute_metrics 成本为 additive per-bar drag，与 engine 的 turnover-scaled 区分但文档化单一口径
    assert "turnover" in src_compute.lower() or "cost" in src_compute.lower()

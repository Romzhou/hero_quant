"""B1 lane TDD: 回测正确性 20 条 (engine/validation/metrics/bench)。中文注释。"""
import pathlib

import numpy as np
import pandas as pd
import pytest


def _prices(n=10, start=100.0, step=10.0):
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    close = [start + i * step for i in range(n)]
    df = pd.DataFrame({"close": close}, index=idx)
    df["open"] = df["close"].shift(1).fillna(df["close"].iloc[0])
    return df


# ---- engine ----
def test_b1_engine_positions_include_leverage():
    """杠杆敞口应进 positions：w=[1,1] 时持仓名义和应约 equity*2。"""
    from hero_quant.backtest.engine import BacktestEngine
    idx = pd.date_range("2024-01-01", periods=5, freq="B")
    df = pd.DataFrame({"A": [100, 101, 102, 103, 104], "B": [50, 51, 52, 53, 54]}, index=idx)
    eng = BacktestEngine(initial_capital=1000.0)
    res = eng.run(df, weights=[1.0, 1.0], costs=0.0, allow_synthetic=True)
    pos = res["positions"]
    eq = res["equity"]
    # 杠杆=2，总权重=2，单资产归一权重*杠杆=1，每列持仓应约 eq
    row_sum = float(pos.iloc[-1].sum())
    eq_last = float(eq.iloc[-1])
    assert row_sum == pytest.approx(eq_last * 2.0, rel=0.05)


def test_b1_engine_aligned_no_timing_shift():
    """对齐收益不应前移一根：单调上涨时末 Bar 收益不应被压成 0。"""
    from hero_quant.backtest.engine import BacktestEngine
    df = _prices(n=5, start=100.0, step=10.0)
    eng = BacktestEngine(initial_capital=1000.0)
    res = eng.run(df, weights=[1.0], costs=0.0, allow_synthetic=True)
    eq = res["equity"]
    last_ret = float(eq.iloc[-1] / eq.iloc[-2] - 1)
    # close 口径末 Bar 约 140/130-1 ≈ 0.0769，不应≈0
    assert last_ret == pytest.approx(140.0 / 130.0 - 1, rel=0.3)
    assert abs(last_ret) > 0.02


def test_b1_engine_turnover_first_day_tolerance():
    """首日换手补齐应用容差：1e-16 近零应视为 0 触发补齐。"""
    from hero_quant.backtest.engine import BacktestEngine
    eng = BacktestEngine()
    idx = pd.date_range("2024-01-01", periods=3, freq="B")
    # 首行和极小，turnover_rate[0] ≈ 1e-16 而非精确 0
    pos_proxy = pd.DataFrame({"position": [1e-16, 1.0, 1.0]}, index=idx)
    gross = pd.Series([1.0, 1.0, 1.0], index=idx)
    out = eng._compute_turnover_rate(pos_proxy, gross, np.array([1.0]), 1.0, leverage=1.0)
    assert float(out.iloc[0]) == pytest.approx(1.0, rel=0.01)


def test_b1_engine_output_atomic(tmp_path, monkeypatch):
    """输出目录应原子写：fills 失败时不应留下部分 positions.csv。"""
    from hero_quant.backtest.engine import BacktestEngine
    df = _prices(n=5)
    eng = BacktestEngine()
    out = tmp_path / "out"
    orig_to_csv = pd.DataFrame.to_csv
    calls = {"n": 0}

    def _flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return orig_to_csv(self, *a, **k)

    monkeypatch.setattr(pd.DataFrame, "to_csv", _flaky)
    with pytest.raises(OSError):
        eng.run(df, weights=[1.0], costs=0.0, allow_synthetic=True, output_dir=str(out))
    # 原子语义：目标目录不应存在部分产物
    assert (not out.exists()) or (not (out / "positions.csv").exists())


# ---- validation ----
def test_b1_validation_rejects_non_datetime_index():
    """非 DatetimeIndex 应 fail-closed 拒绝。"""
    from hero_quant.backtest.validation import ValidationError, validate
    df = pd.DataFrame({"close": [100.0, 101.0, 102.0]}, index=[0, 1, 2])
    with pytest.raises(ValidationError):
        validate(df, weights_on="2024-01-01", price_date="2024-01-05")


def test_b1_validation_int_column_no_raw_attributeerror():
    """整数列不应抛原生 AttributeError，应走 ValidationError。"""
    from hero_quant.backtest.validation import ValidationError, validate
    idx = pd.date_range("2024-01-01", periods=3, freq="B")
    df = pd.DataFrame({0: [100.0, 101.0, -5.0], 1: [50.0, 51.0, 52.0]}, index=idx)
    with pytest.raises(ValidationError):
        validate(df)


def test_b1_validation_single_close_checks_siblings():
    """单 close 分支应同时校验兄弟价格列。"""
    from hero_quant.backtest.validation import ValidationError, validate
    idx = pd.date_range("2024-01-01", periods=3, freq="B")
    df = pd.DataFrame({"close": [100.0, 101.0, 102.0], "AAPL": [10.0, float("nan"), 12.0]}, index=idx)
    with pytest.raises(ValidationError):
        validate(df)


def test_b1_validation_unknown_kwargs_rejected():
    """未知 kwargs 应 fail-closed 拒绝。"""
    from hero_quant.backtest.validation import ValidationError, validate
    df = _prices(n=3)
    with pytest.raises(ValidationError):
        validate(df, weights_onn="2024-01-01")


# ---- metrics ----
def test_b1_metrics_turnover_weights_mean():
    """权重回退应为均值口径：w=[0,1,0,1,0] 应得 0.5 而非 2.0。"""
    from hero_quant.backtest.metrics import turnover
    got = turnover(None, weights=[0, 1, 0, 1, 0])
    assert got == pytest.approx(0.5, rel=0.01)


def test_b1_metrics_turnover_weights_nan_inf():
    """权重含 NaN/Inf 应清洗回落有限值，不泄漏 NaN/Inf。"""
    from hero_quant.backtest.metrics import turnover
    got = turnover(None, weights=[0.0, float("nan"), float("inf"), 1.0])
    assert np.isfinite(got)
    assert got == 0.0


def test_b1_metrics_vol_cumret_sanitize():
    """波动率/累计收益应清洗 Inf/NaN，不泄漏非有限。"""
    from hero_quant.backtest.metrics import compute_metrics
    idx = pd.date_range("2024-01-01", periods=4, freq="B")
    s = pd.Series([1.0, 0.0, 1.0, 2.0], index=idx)
    m = compute_metrics(s)
    assert np.isfinite(m["volatility"])
    assert np.isfinite(m["cumulative_return"])
    assert m["volatility"] == 0.0 or np.isfinite(m["volatility"])


def test_b1_metrics_sharpe_zero_tolerance():
    """Sharpe 零判断应用容差：极小波动应视为 0。"""
    from hero_quant.backtest.metrics import sharpe_ratio
    idx = pd.date_range("2024-01-01", periods=5, freq="B")
    s = pd.Series([1.0, 1.0 + 1e-13, 1.0 + 2e-13, 1.0 + 3e-13, 1.0 + 4e-13], index=idx)
    assert sharpe_ratio(s) == 0.0


def test_b1_metrics_annual_return_coerce():
    """annual_return 脏数据应回落 0 而非抛错。"""
    from hero_quant.backtest.metrics import annual_return
    s = pd.Series(["a", "bad", None, "xx"])
    assert annual_return(s) == 0.0
    s2 = pd.Series([100.0, float("nan"), 110.0, 121.0])
    assert np.isfinite(annual_return(s2))


# ---- bench ----
def test_b1_bench_strategy_failure_marked(monkeypatch):
    """策略腿失败不应静默零指标，应传播或标记 failed。"""
    from hero_quant.backtest import bench as bench_mod
    from hero_quant.backtest.engine import BacktestEngine

    def _boom(self, *a, **k):
        raise ValueError("engine boom")

    monkeypatch.setattr(BacktestEngine, "run", _boom)
    try:
        bench_mod.run_batch(["AAA"], dates=["2024-01-01", "2024-01-02"], allow_synthetic=True)
    except (ValueError, RuntimeError):
        return
    # 若选择不抛，则必须带 failed/error 标记（此处视为未修复）
    pytest.fail("engine failure was swallowed into zero metrics without raise")


def test_b1_bench_benchmark_failure_marked(monkeypatch):
    """基准腿失败应标记 benchmark_error/failed，alpha 不可为有效值。"""
    from hero_quant.backtest import bench as bench_mod
    from hero_quant.backtest.engine import BacktestEngine
    ok = {"metrics": {"cumulative_return": 0.05, "sharpe": 1.0}}
    calls = {"n": 0}

    def _fake(self, prices, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("bench boom")
        return ok

    monkeypatch.setattr(BacktestEngine, "run", _fake)
    res = bench_mod.run_batch(["AAA"], dates=["2024-01-01", "2024-01-02", "2024-01-03"], allow_synthetic=True)
    enriched = res["AAA"]
    assert enriched.get("failed") is True or "benchmark_error" in enriched or enriched.get("alpha") is None


def test_b1_bench_allow_synthetic_hoisted():
    """空输入也应执行 fail-closed：run_batch([], allow_synthetic=False) 应抛错。"""
    from hero_quant.backtest import bench as bench_mod
    with pytest.raises(ValueError):
        bench_mod.run_batch([], dates=None, allow_synthetic=False)


def test_b1_bench_disclosure_has_marker(monkeypatch):
    """委托 news.get_disclosure 缺标记时应补 non-PIT 标记。"""
    import hero_quant.data.loaders.news as news_mod
    from hero_quant.backtest import bench as bench_mod
    monkeypatch.setattr(news_mod, "get_disclosure", lambda recs: "PIT only text")
    txt = bench_mod._build_pit_disclosure([{"title": "x"}])
    assert "non-PIT" in txt


def test_b1_bench_traversal_fail_closed(monkeypatch):
    """safe_join 拒绝应抛出而非 pass 吞掉。"""
    from hero_quant.backtest import bench as bench_mod
    import hero_quant.security.sanitize as san
    monkeypatch.setattr(san, "safe_join", lambda b, p: (_ for _ in ()).throw(ValueError("bad")))
    with pytest.raises(ValueError):
        bench_mod.run_batch(["AAA"], dates=["2024-01-01", "2024-01-02"], allow_synthetic=True, output_dir="foo")


def test_b1_bench_no_dead_kwargs():
    """死 kwargs 分支应删除：kwargs.pop('dates'/'news_records') 不可达。"""
    src = pathlib.Path("src/hero_quant/backtest/bench.py").read_text(encoding="utf-8")
    assert 'kwargs.pop("dates"' not in src and "kwargs.pop('dates'" not in src
    assert 'kwargs.pop("news_records"' not in src and "kwargs.pop('news_records'" not in src


def test_b1_bench_benchmark_cached(monkeypatch):
    """基准解析应复用缓存：多 ticker 不应每 ticker 重建 Settings。"""
    from hero_quant.backtest import bench as bench_mod
    import hero_quant.config.settings as settings_mod
    count = {"n": 0}
    OrigSettings = settings_mod.Settings

    def _counting(*a, **k):
        count["n"] += 1
        return OrigSettings(*a, **k)

    monkeypatch.setattr(settings_mod, "Settings", _counting)
    # 同步 bench 内部引用（from-import 已绑定类，需补丁 bench 命名空间无用，按模块补丁即可触发）
    bench_mod.run_batch(["A", "B", "C"], dates=["2024-01-01", "2024-01-02"], allow_synthetic=True)
    assert count["n"] <= 2

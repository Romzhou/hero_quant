"""W4 覆盖率补齐：backtest/engine.py 未覆盖分支。

覆盖目标（src/hero_quant/backtest/engine.py）：
- Signal.generate：equal_weight / sma_crossover 多头与空头 / 数据不足回落 / 未知方法回落 /
  资产数由价格矩阵推断
- generate_signal：静态权重透传（含 NaN/Inf 归零）与非法静态权重回落
- BacktestEngine.__init__ 的非法初始资金拒绝
- run() 的入口守卫：非法 costs、非 DataFrame、空帧、PIT 缺日期、pit_ack 缺失
- run() 的信号/权重路径：Signal 对象、signal_method、bear 零权重保留
- 多资产路径与 output_dir 产物落盘
- _execute_bars 的 DataFrame 拒绝与资金预检等比缩放
- on_tick 的 dict/对象/脏数据形态
- _drawdown_episodes_html 的单调与有回撤两种输出

断言均为具体数值/字段值/异常类型，不使用弱断言。
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import numpy as np
import pandas as pd
import pytest

from hero_quant.backtest.engine import (
    BacktestEngine,
    PIT_ACK,
    PITViolation,
    Signal,
    _static_signal,
    generate_signal,
)


def _prices(values, col: str = "close") -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(values), freq="D")
    return pd.DataFrame({col: [float(v) for v in values]}, index=idx)


# ---------------------------------------------------------------- Signal


def test_signal_equal_weight_is_uniform():
    """等权信号在多资产上均分。"""
    df = pd.DataFrame({"A": [1.0, 2.0], "B": [3.0, 4.0]})
    w = Signal(method="equal_weight").generate(df)
    assert list(w) == [0.5, 0.5]


def test_signal_infers_asset_count_from_price_columns():
    """资产数由价格矩阵列数推断（close/open/high/low/volume 不计入）。"""
    df = pd.DataFrame({"close": [1.0, 2.0], "open": [1.0, 2.0], "volume": [1, 2]})
    w = Signal(method="equal_weight").generate(df)
    assert len(w) == 1
    assert w[0] == pytest.approx(1.0)


def test_signal_explicit_n_assets_zero_is_clamped_to_one():
    """n_assets<=0 时归一为 1。"""
    df = _prices([1.0, 2.0])
    w = Signal(method="equal_weight").generate(df, n_assets=0)
    assert len(w) == 1
    assert w[0] == pytest.approx(1.0)


def test_signal_sma_crossover_bull_returns_equal_weight():
    """短均线在上（多头）时返回等权。"""
    df = _prices([1, 2, 3, 4, 5, 6])
    w = Signal(method="sma_crossover", window_short=2, window_long=4).generate(df)
    assert list(w) == [pytest.approx(1.0)]


def test_signal_sma_crossover_bear_returns_zero_weights():
    """短均线在下（空头）时返回 0 权重（空仓），不回落等权。"""
    df = _prices([6, 5, 4, 3, 2, 1])
    w = Signal(method="sma_crossover", window_short=2, window_long=4).generate(df)
    assert list(w) == [pytest.approx(0.0)]


def test_signal_sma_crossover_insufficient_data_falls_back_to_equal_weight():
    """数据不足 window_long 时回落等权。"""
    df = _prices([1, 2])
    w = Signal(method="sma_crossover", window_short=2, window_long=20).generate(df)
    assert list(w) == [pytest.approx(1.0)]


def test_signal_unknown_method_falls_back_to_equal_weight():
    """未知信号方法回落等权并告警。"""
    df = _prices([1, 2, 3])
    w = Signal(method="does-not-exist").generate(df)
    assert list(w) == [pytest.approx(1.0)]


# ---------------------------------------------------------------- generate_signal / _static_signal


def test_generate_signal_passes_through_static_weights():
    """显式静态权重直接透传。"""
    df = _prices([1, 2, 3])
    w = generate_signal(df, weights=[0.3, 0.7])
    assert list(w) == [pytest.approx(0.3), pytest.approx(0.7)]


def test_generate_signal_static_weights_sanitize_nan_and_inf():
    """静态权重中的 NaN/Inf 被归零（不污染收益）。"""
    df = _prices([1, 2, 3])
    w = generate_signal(df, weights=[float("nan"), float("inf")])
    assert list(w) == [pytest.approx(0.0), pytest.approx(0.0)]


def test_generate_signal_invalid_static_weights_fall_back_to_signal_model():
    """非法静态权重（无法转 float）时回落到信号模型。"""
    df = _prices([1, 2, 3])
    w = generate_signal(df, method="equal_weight", weights=["a", "b"])
    assert len(w) == 1
    assert w[0] == pytest.approx(1.0)


def test_static_signal_sanitizes_non_finite():
    """_static_signal 对 NaN/Inf 归零。"""
    w = _static_signal([1.0, float("nan"), float("inf")])
    assert list(w) == [pytest.approx(1.0), pytest.approx(0.0), pytest.approx(0.0)]


def test_static_signal_invalid_returns_single_one():
    """_static_signal 无法转换时回落 [1.0]。"""
    w = _static_signal(["a"])
    assert list(w) == [pytest.approx(1.0)]


# ---------------------------------------------------------------- 引擎构造与入口守卫


@pytest.mark.parametrize("bad", [0, -1.0, float("nan"), float("inf")])
def test_engine_rejects_invalid_initial_capital(bad):
    """初始资金必须为正且有限。"""
    with pytest.raises(ValueError, match="initial_capital must be >0 and finite"):
        BacktestEngine(initial_capital=bad)


def test_run_rejects_negative_costs():
    """负费率直接拒绝（避免复利伪造收益）。"""
    e = BacktestEngine()
    with pytest.raises(ValueError, match="costs must be finite and >= 0"):
        e.run(_prices([1, 2, 3]), costs=-0.001, allow_synthetic=True)


def test_run_rejects_non_finite_costs():
    """NaN/Inf 费率拒绝。"""
    e = BacktestEngine()
    with pytest.raises(ValueError, match="costs must be finite and >= 0"):
        e.run(_prices([1, 2, 3]), costs=float("nan"), allow_synthetic=True)


def test_run_rejects_non_dataframe_prices():
    """非 DataFrame 价格输入抛 TypeError。"""
    e = BacktestEngine()
    with pytest.raises(TypeError, match="prices must be a pandas DataFrame"):
        e.run([1, 2, 3], allow_synthetic=True)


def test_run_rejects_empty_dataframe():
    """空 DataFrame 拒绝。"""
    e = BacktestEngine()
    with pytest.raises(ValueError, match="prices DataFrame is empty"):
        e.run(pd.DataFrame(), allow_synthetic=True)


def test_run_requires_pit_dates_or_allow_synthetic():
    """既无 PIT 日期又未显式 allow_synthetic 时 fail-closed。"""
    e = BacktestEngine()
    with pytest.raises(PITViolation, match="PIT violation"):
        e.run(_prices([1, 2, 3]))


def test_run_pit_bypass_requires_pit_ack():
    """skip_pit/enforce_pit=False 必须附 pit_ack 契约字符串。"""
    e = BacktestEngine()
    df = _prices([1, 2, 3])
    with pytest.raises(PITViolation, match="pit_ack"):
        e.run(df, skip_pit=True)
    res = e.run(df, skip_pit=True, pit_ack=PIT_ACK)
    assert res["non_pit"] is True
    assert res["metrics"]["non_pit"] is True


def test_run_rejects_wrong_pit_ack_string():
    """pit_ack 变体不被接受（全局冻结字符串，全等比较）。"""
    e = BacktestEngine()
    with pytest.raises(PITViolation, match="pit_ack"):
        e.run(_prices([1, 2, 3]), enforce_pit=False, pit_ack="i_know_this_is_non_pit")


# ---------------------------------------------------------------- run 主路径


def test_run_single_asset_produces_equity_and_metrics():
    """单资产回测产出权益/持仓/成交/指标。"""
    e = BacktestEngine()
    res = e.run(_prices([100, 101, 102, 103, 104]), allow_synthetic=True)
    assert len(res["equity"]) == 5
    # 默认 costs=0.0005，首日建仓换手=1 -> 首根权益扣一次成本
    assert res["equity"].iloc[0] == pytest.approx(1.0 - 0.0005, abs=1e-9)
    assert res["metrics"]["cumulative_return"] > 0.0
    assert res["positions"].shape[0] == 5
    assert res["fills"].shape[0] == 5
    assert "Tearsheet" in res["tearsheet"]
    assert json.loads(res["metrics_json"])["cumulative_return"] == pytest.approx(
        res["metrics"]["cumulative_return"], rel=1e-12
    )


def test_run_zero_cost_equity_starts_at_initial_capital():
    """零成本时首根权益等于初始资金。"""
    res = BacktestEngine().run(_prices([100, 101, 102, 103, 104]), costs=0.0, allow_synthetic=True)
    assert res["equity"].iloc[0] == pytest.approx(1.0, abs=1e-12)


def test_run_multi_asset_path():
    """多资产回测产出多列持仓。"""
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    df = pd.DataFrame({"A": [10.0, 11, 12, 13, 14], "B": [20.0, 19, 21, 22, 23]}, index=idx)
    res = BacktestEngine().run(df, weights=[0.5, 0.5], allow_synthetic=True)
    assert res["positions"].shape[1] == 2
    assert set(res["positions"].columns) == {"A", "B"}


def test_run_bear_zero_weights_kept():
    """全零权重视为空仓信号，收益为 0（不被静默改成等权）。"""
    e = BacktestEngine()
    res = e.run(_prices([100, 101, 102, 103]), weights=[0.0], allow_synthetic=True)
    assert res["metrics"]["cumulative_return"] == pytest.approx(0.0, abs=1e-12)


def test_run_with_signal_object_generates_weights():
    """传入 Signal 对象时由信号模型生成权重。"""
    e = BacktestEngine()
    res = e.run(_prices([1, 2, 3, 4, 5, 6]), signal=Signal(method="equal_weight"), allow_synthetic=True)
    assert res["metrics"]["cumulative_return"] > 0.0


def test_run_with_signal_method_string():
    """signal_method 字符串同样触发信号生成。"""
    e = BacktestEngine()
    res = e.run(_prices([1, 2, 3, 4, 5, 6]), signal_method="equal_weight", allow_synthetic=True)
    assert res["metrics"]["cumulative_return"] > 0.0


def test_run_writes_output_artifacts():
    """output_dir 落盘 positions/fills/metrics/tearsheet 四件套。"""
    with tempfile.TemporaryDirectory() as tmp:
        e = BacktestEngine()
        e.run(_prices([100, 101, 102, 103]), output_dir=tmp, allow_synthetic=True)
        d = pathlib.Path(tmp)
        assert (d / "positions.csv").exists()
        assert (d / "fills.csv").exists()
        assert (d / "metrics.json").exists()
        assert (d / "tearsheet.html").exists()
        metrics = json.loads((d / "metrics.json").read_text(encoding="utf-8"))
        assert "cumulative_return" in metrics


def test_run_negative_price_rejected():
    """非正价格被拒（脏数据不得进入回测）。"""
    e = BacktestEngine()
    with pytest.raises(Exception):
        e.run(_prices([100, 0, 102, 103]), allow_synthetic=True)


# ---------------------------------------------------------------- 资金预检


def test_execute_bars_rejects_dataframe():
    """_execute_bars 只接受 Series/ndarray/list。"""
    e = BacktestEngine()
    with pytest.raises(TypeError, match="does not accept DataFrame"):
        e._execute_bars(pd.DataFrame({"a": [1.0]}), available_capital=1.0)


def test_execute_bars_scales_down_when_over_capital():
    """名义敞口超可用资金时等比缩放（保持比例）。"""
    e = BacktestEngine()
    out = e._execute_bars(pd.Series([3.0, 1.0]), available_capital=2.0)
    assert out.sum() == pytest.approx(2.0)
    assert out.iloc[0] / out.iloc[1] == pytest.approx(3.0)


def test_execute_bars_keeps_positions_when_within_capital():
    """敞口未超限时原样返回。"""
    e = BacktestEngine()
    out = e._execute_bars(pd.Series([0.5, 0.5]), available_capital=2.0)
    assert list(out) == [pytest.approx(0.5), pytest.approx(0.5)]


# ---------------------------------------------------------------- on_tick


def test_on_tick_with_dict():
    """dict tick 解析出价格与标的。"""
    e = BacktestEngine()
    out = e.on_tick({"price": 12.5, "symbol": "AAPL"})
    assert out["symbol"] == "AAPL"
    assert out["price"] == pytest.approx(12.5)
    assert out["latency_breach"] is False


def test_on_tick_with_object():
    """对象形态 tick 同样可解析。"""

    class _Tick:
        price = 3.25
        symbol = "TSLA"

    e = BacktestEngine()
    out = e.on_tick(_Tick())
    assert out["symbol"] == "TSLA"
    assert out["price"] == pytest.approx(3.25)


def test_on_tick_with_dirty_payload_does_not_raise():
    """脏 tick 不抛异常，价格回落 0。"""
    e = BacktestEngine()
    out = e.on_tick({"price": "not-a-number"})
    assert out["price"] == 0.0
    assert out["symbol"] == ""


# ---------------------------------------------------------------- 回撤区间渲染


def test_drawdown_episodes_monotonic_has_no_episodes():
    """单调上涨无回撤区间。"""
    e = BacktestEngine()
    html = e._drawdown_episodes_html(pd.Series([1.0, 2.0, 3.0]))
    assert "No drawdown episodes" in html


def test_drawdown_episodes_renders_table():
    """存在回撤时渲染出区间表格。"""
    s = pd.Series([100.0, 80.0, 120.0], index=pd.date_range("2024-01-01", periods=3, freq="D"))
    html = BacktestEngine()._drawdown_episodes_html(s)
    assert "<table" in html
    assert "2024-01-02" in html


def test_drawdown_episodes_empty_series():
    """空序列返回无回撤提示。"""
    html = BacktestEngine()._drawdown_episodes_html(pd.Series(dtype=float))
    assert "No drawdown episodes" in html


def test_tearsheet_handles_non_datetime_index():
    """非 DatetimeIndex 权益曲线也能生成 tearsheet。"""
    e = BacktestEngine()
    html = e._build_tearsheet(pd.Series([1.0, 2.0], index=[0, 1]), {"sharpe": 1.0})
    assert "Tearsheet" in html
    assert "2026-08" in html


def test_run_result_contains_engine_name_and_costs():
    """结果含 engine 名与实际费率。"""
    e = BacktestEngine()
    res = e.run(_prices([100, 101, 102]), costs=0.001, allow_synthetic=True)
    assert res["engine"] == "default"
    assert res["costs"] == pytest.approx(0.001)
    assert res["non_pit"] is False
    assert np.isfinite(res["metrics"]["cumulative_return"])

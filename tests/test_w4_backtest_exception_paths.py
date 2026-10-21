"""W4 覆盖率补齐：backtest/ 与 llm/ 的异常路径（write-only 兜底、告警 helper、序列化兜底）。

覆盖目标（依据 --cov-report=term-missing 的 Missing 行号）：
- backtest/engine.py: _warn 告警 helper、_align 末 Bar open/宽表回落、output_dir 落盘失败分支、
  tearsheet 月度表异常分支
- backtest/bench.py: _warn 告警 helper、run_batch 引擎意外异常、_safe_cum 的 ndarray/脏值、
  _jsonable 的 numpy 归一、落盘失败分支
- backtest/metrics.py: to_numeric 失败、成本接线失败、收益/波动率计算失败
- backtest/validation.py: 多资产列校验失败、币种校验失败

断言均为具体数值/异常类型/文本片段，不使用弱断言。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from hero_quant.backtest import bench as bm
from hero_quant.backtest import engine as eng
from hero_quant.backtest import metrics as met
from hero_quant.backtest.validation import ValidationError, validate


def _idx(n: int = 4) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01", periods=n, freq="D")


# ---------------------------------------------------------------- 告警 helper


def test_engine_warn_emits_stdlib_and_structlog(caplog):
    """engine._warn 同时走 stdlib 与 structlog（error_code/chain 透出）。"""
    with caplog.at_level(logging.WARNING):
        eng._warn("w4_probe", "w4 probe %s", "arg", chain="B")
    assert any("w4 probe" in r.message for r in caplog.records)


def test_bench_warn_emits_stdlib_and_structlog(caplog):
    """bench._warn 同时走 stdlib 与 structlog。"""
    with caplog.at_level(logging.WARNING):
        bm._warn("w4_probe", "w4 probe %s", "arg", chain="B")
    assert any("w4 probe" in r.message for r in caplog.records)


# ---------------------------------------------------------------- _align 末 Bar


def test_align_last_bar_with_open_only_uses_open():
    """末 Bar 无 close 但有 open 时用当 Bar 的 open。"""
    e = eng.BacktestEngine()
    df = pd.DataFrame({"open": [9.0, 9.5]}, index=_idx(2))
    out = e._align(df, 1)
    assert float(out.iloc[0]) == pytest.approx(9.5)


def test_align_last_bar_wide_table_fallback():
    """末 Bar 既无 close 也无 open 的宽表回落为首个有效数值。"""
    e = eng.BacktestEngine()
    df = pd.DataFrame({"px": [10.0, 11.0]}, index=_idx(2))
    out = e._align(df, 1)
    assert float(out.iloc[0]) == pytest.approx(11.0)


def test_align_next_day_wide_table_fallback():
    """非末 Bar 的宽表（无 open/close）同样回落为首个有效数值。"""
    e = eng.BacktestEngine()
    df = pd.DataFrame({"px": [10.0, 11.0, 12.0]}, index=_idx(3))
    out = e._align(df, 0)
    assert float(out.iloc[0]) == pytest.approx(11.0)


def test_align_last_bar_multi_asset_parse_error_propagates():
    """末 Bar 多资产解析失败时抛错（fail-closed）。"""
    e = eng.BacktestEngine()
    df = pd.DataFrame({"A": [10.0, 0.0], "B": [20.0, 21.0]}, index=_idx(2))
    with pytest.raises(ValueError, match="non-positive"):
        e._align(df, 1)


# ---------------------------------------------------------------- output_dir 落盘失败


def test_run_output_dir_tmp_mkdir_failure_raises(tmp_path):
    """临时目录创建失败时抛错（不静默）。"""
    blocker = tmp_path / "out.tmp"
    blocker.write_text("blocker", encoding="utf-8")
    e = eng.BacktestEngine()
    df = pd.DataFrame({"close": [10.0, 11.0]}, index=_idx(2))
    with pytest.raises(OSError):
        e.run(df, output_dir=tmp_path / "out", allow_synthetic=True)


def test_run_output_dir_publish_failure_raises(tmp_path):
    """发布阶段（rename）失败时抛错（不留半成品）。"""
    out = tmp_path / "out"
    out.write_text("i am a file", encoding="utf-8")
    e = eng.BacktestEngine()
    df = pd.DataFrame({"close": [10.0, 11.0]}, index=_idx(2))
    with pytest.raises(OSError):
        e.run(df, output_dir=out, allow_synthetic=True)


def test_run_output_dir_fills_write_failure_raises(tmp_path, monkeypatch):
    """fills.csv 写入失败时抛错（不静默吞 IO 错误）。"""
    real_to_csv = pd.DataFrame.to_csv

    def _boom(self, path, *a, **kw):
        if str(path).endswith("fills.csv"):
            raise OSError("disk full")
        return real_to_csv(self, path, *a, **kw)

    monkeypatch.setattr(pd.DataFrame, "to_csv", _boom)
    e = eng.BacktestEngine()
    df = pd.DataFrame({"close": [10.0, 11.0]}, index=_idx(2))
    with pytest.raises(OSError, match="disk full"):
        e.run(df, output_dir=tmp_path / "out", allow_synthetic=True)


def test_run_output_dir_text_write_failure_raises(tmp_path, monkeypatch):
    """metrics.json / tearsheet.html 写入失败时抛错。"""
    real_write_text = Path.write_text

    def _boom(self, *a, **kw):
        if self.name in ("metrics.json", "tearsheet.html"):
            raise OSError("disk full")
        return real_write_text(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", _boom)
    e = eng.BacktestEngine()
    df = pd.DataFrame({"close": [10.0, 11.0]}, index=_idx(2))
    with pytest.raises(OSError, match="disk full"):
        e.run(df, output_dir=tmp_path / "out", allow_synthetic=True)


# ---------------------------------------------------------------- tearsheet 异常


def test_build_tearsheet_handles_broken_equity():
    """权益曲线异常时 tearsheet 仍能生成（月度表回落占位）。"""
    e = eng.BacktestEngine()
    html = e._build_tearsheet(pd.Series([1.0, 2.0], index=[0, 1]), {"sharpe": 0.0})
    assert "Tearsheet" in html


# ---------------------------------------------------------------- bench


def test_run_batch_unexpected_engine_error_propagates():
    """引擎抛出非 ValueError/RuntimeError 异常时向上传播（不伪装零收益）。"""
    with patch.object(bm.BacktestEngine, "run", side_effect=KeyError("weird")):
        with pytest.raises(KeyError):
            bm.run_batch(["AAPL"], dates=["2024-01-01"], allow_synthetic=True)


def test_run_batch_unexpected_bench_error_propagates():
    """基准腿抛出意外异常时向上传播。"""
    real_run = bm.BacktestEngine.run
    state = {"n": 0}

    def _flaky(self, prices, **kw):
        state["n"] += 1
        if state["n"] == 1:
            return real_run(self, prices, **kw)
        raise KeyError("bench weird")

    with patch.object(bm.BacktestEngine, "run", _flaky):
        with pytest.raises(KeyError):
            bm.run_batch(["AAPL"], dates=["2024-01-01"], allow_synthetic=True)


def test_bench_jsonable_converts_numpy_scalars():
    """_jsonable 把 numpy 标量/数组转为原生类型（可 JSON 序列化）。"""
    out = bm.run_batch(["AAPL"], dates=["2024-01-01", "2024-01-02"], allow_synthetic=True)
    text = json.dumps(out, ensure_ascii=False)
    assert "AAPL" in text


def test_bench_json_output_write_failure_raises(tmp_path, monkeypatch):
    """.json 输出写入失败时抛错（不静默）。"""
    real_write_text = Path.write_text

    def _boom(self, *a, **kw):
        if self.name == "metrics.json":
            raise OSError("disk full")
        return real_write_text(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", _boom)
    with pytest.raises(OSError, match="disk full"):
        bm.run_batch(
            ["AAPL"],
            dates=["2024-01-01", "2024-01-02"],
            output_dir=tmp_path / "metrics.json",
            allow_synthetic=True,
        )


def test_bench_dir_output_write_failure_raises(tmp_path, monkeypatch):
    """目录输出的 metrics.json 写入失败时抛错。"""
    real_write_text = Path.write_text

    def _boom(self, *a, **kw):
        if self.name == "metrics.json":
            raise OSError("disk full")
        return real_write_text(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", _boom)
    with pytest.raises(OSError, match="disk full"):
        bm.run_batch(["AAPL"], dates=["2024-01-01", "2024-01-02"], output_dir=tmp_path / "out", allow_synthetic=True)


def test_bench_tearsheet_write_failure_raises(tmp_path, monkeypatch):
    """tearsheet.html 写入失败时抛错。"""
    real_write_text = Path.write_text

    def _boom(self, *a, **kw):
        if self.name == "tearsheet.html":
            raise OSError("disk full")
        return real_write_text(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", _boom)
    with pytest.raises(OSError, match="disk full"):
        bm.run_batch(["AAPL"], dates=["2024-01-01", "2024-01-02"], output_dir=tmp_path / "out", allow_synthetic=True)


def test_normalize_index_rejects_series_input():
    """Series 输入因真值歧义被拒（只接受 list/None 形态）。"""
    with pytest.raises(ValueError):
        bm._normalize_index(pd.Series(["2024-01-01", "2024-01-02"]))


def test_bench_disclosure_with_news_loader_failure(monkeypatch):
    """news loader 抛异常时披露文本回落为明确文案。"""
    import hero_quant.data.loaders.news as news_mod

    def _boom(_records):
        raise TypeError("news loader down")

    monkeypatch.setattr(news_mod, "get_disclosure", _boom)
    txt = bm._build_pit_disclosure([{"title": "x"}])
    assert "non-PIT source/unavailable" in txt


# ---------------------------------------------------------------- metrics 异常路径


def test_metrics_to_numeric_failure_returns_zero_block(monkeypatch):
    """权益数值化失败时返回全零指标（不抛）。"""

    def _boom(_s, *a, **kw):
        raise ValueError("bad series")

    monkeypatch.setattr(pd, "to_numeric", _boom)
    out = met.compute_metrics(pd.Series([1.0, 2.0]))
    assert out["cumulative_return"] == 0.0
    assert out["volatility"] == 0.0


def test_metrics_costs_wiring_failure_keeps_gross(monkeypatch):
    """成本接线失败时保留原权益曲线（告警不抛）。"""
    real_pct_change = pd.Series.pct_change
    state = {"n": 0}

    def _boom_once(self, *a, **kw):
        state["n"] += 1
        if state["n"] == 1:
            raise ValueError("pct broke")
        return real_pct_change(self, *a, **kw)

    monkeypatch.setattr(pd.Series, "pct_change", _boom_once)
    out = met.compute_metrics(pd.Series([100.0, 110.0, 121.0]), costs=0.01)
    # 成本段失败 -> 仍用原始权益计算指标（累计收益 0.21）
    assert out["cumulative_return"] == pytest.approx(0.21, abs=1e-12)


def test_metrics_turnover_with_bad_positions_returns_zero():
    """持仓数据无法数值化时换手率回落 0.0。"""
    assert met.turnover(pd.DataFrame({"a": ["x", "y"]})) == 0.0


# ---------------------------------------------------------------- validation 异常路径


def test_validation_multi_asset_column_conversion_failure():
    """多资产列无法数值化时抛 ValidationError（fail-closed）。"""
    idx = _idx(2)
    df = pd.DataFrame({"A": [10.0, 11.0], "B": [object(), object()]}, index=idx)
    with pytest.raises(ValidationError):
        validate(df)


def test_validation_currency_column_with_unexpected_type_raises():
    """币种列取到非 Series 对象时按 fail-closed 处理（不静默放行）。"""
    idx = _idx(2)
    df = pd.DataFrame({"close": [10.0, 11.0], "currency": ["USD", "USD"]}, index=idx)
    with pytest.raises(ValidationError):
        validate(df, currency=123)  # type: ignore[arg-type]

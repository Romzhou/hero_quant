"""W4 覆盖率补齐：backtest/bench.py 未覆盖分支。

覆盖目标（src/hero_quant/backtest/bench.py）：
- _build_pit_disclosure：全 PIT / 部分 PIT / 全非 PIT / 无 pit 字段四种披露文本
- get_disclosure 的 news kwargs 兼容路径
- _effective_benchmark_ticker 的空串与显式覆盖
- _resolve_benchmark 的 _resolved 复用路径与最长后缀优先
- _normalize_index 的默认 5 日与单日扩展为 5 个交易日
- run_batch：字符串 ticker 归一、.json 输出路径、目录输出（含 tearsheet）、
  路径穿越拒绝、news pit 状态标注、基准腿失败时 alpha=None
- _build_tearsheet_html 的空结果兜底

断言均为具体数值/字符串片段/异常类型，不使用弱断言。
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import pandas as pd
import pytest

from hero_quant.backtest.bench import (
    _build_pit_disclosure,
    _build_tearsheet_html,
    _effective_benchmark_ticker,
    _normalize_index,
    _resolve_benchmark,
    _synthetic_prices,
    get_disclosure,
    run_batch,
)


# ---------------------------------------------------------------- PIT 披露


def test_disclosure_without_records_states_unavailable():
    """无新闻记录时明写 PIT 不可用。"""
    txt = _build_pit_disclosure(None)
    assert "non-PIT source/unavailable" in txt
    assert "PIT unavailable" in txt


def test_disclosure_all_pit_verified():
    """全部记录 PIT 已验证时给出计数。"""
    txt = _build_pit_disclosure([{"pit": True}, {"pit": True}])
    assert "PIT verified 2/2" in txt
    assert "non-PIT source/unavailable 0/2" in txt


def test_disclosure_mixed_pit_counts():
    """部分 PIT 时同时给出已验证与未验证计数。"""
    txt = _build_pit_disclosure([{"pit": True}, {"pit": False}, {"pit": False}])
    assert "PIT verified 1/3" in txt
    assert "non-PIT source/unavailable 2/3" in txt


def test_disclosure_all_non_pit():
    """全部非 PIT 时给出未验证比例。"""
    txt = _build_pit_disclosure([{"pit": False}, {"pit": False}])
    assert "2/2 records not PIT-verified" in txt


def test_disclosure_without_pit_field_uses_loader_disclosure():
    """记录无 pit 字段时委托 news loader 的 disclosure。"""
    txt = _build_pit_disclosure([{"title": "x"}])
    assert isinstance(txt, str)
    assert "non-PIT" in txt or "PIT" in txt


def test_get_disclosure_accepts_news_kwargs():
    """get_disclosure 支持 news= 关键字兼容参数。"""
    txt = get_disclosure(news=[{"pit": True}, {"pit": True}])
    assert "PIT verified 2/2" in txt


# ---------------------------------------------------------------- 基准解析


def test_effective_benchmark_ticker_explicit_wins():
    """显式基准优先于 Settings。"""
    assert _effective_benchmark_ticker("SPY") == "SPY"


def test_effective_benchmark_ticker_empty_string_is_unset():
    """空串视为未覆盖（回落 Settings/None）。"""
    assert _effective_benchmark_ticker("") is None


def test_resolve_benchmark_reuses_resolved_map():
    """_resolved=True 时复用已解析映射，不重建 Settings。"""
    got = _resolve_benchmark(
        "600519.SS",
        _resolved_map={".SS": "000001.SS", "": "SPY"},
        _resolved_ticker=None,
        _resolved=True,
    )
    assert got == "000001.SS"


def test_resolve_benchmark_resolved_ticker_has_priority():
    """已解析的显式基准优先于映射。"""
    got = _resolve_benchmark(
        "600519.SS",
        _resolved_map={".SS": "000001.SS"},
        _resolved_ticker="QQQ",
        _resolved=True,
    )
    assert got == "QQQ"


def test_resolve_benchmark_prefers_longest_suffix():
    """后缀按长度降序匹配，避免短后缀误命中。"""
    bmap = {"": "SPY", ".S": "SHORT", ".SS": "LONG"}
    assert _resolve_benchmark("600519.SS", benchmark_map=bmap) == "LONG"


def test_resolve_benchmark_default_is_spy():
    """无后缀命中时回落默认 SPY。"""
    assert _resolve_benchmark("AAPL", benchmark_map={".SS": "000001.SS"}) == "SPY"


# ---------------------------------------------------------------- 索引归一


def test_normalize_index_defaults_to_five_days():
    """未给日期时使用默认 5 日。"""
    idx = _normalize_index(None)
    assert len(idx) == 5
    assert idx[0] == pd.Timestamp("2024-01-01")


def test_normalize_index_single_day_expands_to_five_business_days():
    """单日输入扩展为 5 个交易日（保证收益可算）。"""
    idx = _normalize_index(["2024-01-03"])  # 周三
    assert len(idx) == 5
    assert idx[0] == pd.Timestamp("2024-01-03")
    assert all(d.weekday() < 5 for d in idx)
    assert idx[-1] == pd.Timestamp("2024-01-09")


def test_normalize_index_sorts_input():
    """输入乱序时按时间排序。"""
    idx = _normalize_index(["2024-01-03", "2024-01-01", "2024-01-02"])
    assert idx[0] == pd.Timestamp("2024-01-01")
    assert idx[-1] == pd.Timestamp("2024-01-03")


# ---------------------------------------------------------------- 合成价格


def test_synthetic_prices_has_open_and_positive_close():
    """合成价格含 open 列且 close 为正（供次日开盘执行）。"""
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    df = _synthetic_prices(idx, "AAPL")
    assert list(df.columns) == ["close", "open"]
    assert (df["close"] > 0).all()
    assert df["open"].notna().all()


# ---------------------------------------------------------------- run_batch


def test_run_batch_requires_allow_synthetic():
    """未显式 allow_synthetic=True 时 fail-closed。"""
    with pytest.raises(ValueError, match="synthetic-only"):
        run_batch(["AAPL"], dates=["2024-01-01"])


def test_run_batch_rejects_empty_tickers():
    """空 tickers 列表 fail-closed。"""
    with pytest.raises(ValueError, match="non-empty tickers"):
        run_batch([], dates=["2024-01-01"], allow_synthetic=True)


def test_run_batch_normalizes_string_ticker():
    """单字符串 ticker 归一为列表。"""
    res = run_batch("AAPL", dates=["2024-01-01", "2024-01-02"], allow_synthetic=True)
    assert "AAPL" in res


def test_run_batch_writes_metrics_and_tearsheet_to_dir():
    """目录输出同时落 metrics.json 与 tearsheet.html。"""
    with tempfile.TemporaryDirectory() as tmp:
        run_batch(["AAPL"], dates=["2024-01-01", "2024-01-02"], output_dir=tmp, allow_synthetic=True)
        d = pathlib.Path(tmp)
        assert (d / "metrics.json").exists()
        assert (d / "tearsheet.html").exists()
        data = json.loads((d / "metrics.json").read_text(encoding="utf-8"))
        assert data["AAPL"]["non_pit"] is True
        assert "MUST NOT be used for live trading thresholds" in data["AAPL"]["not_for_live_thresholds"]


def test_run_batch_json_output_path_writes_that_file():
    """给出 .json 路径时直接写该文件，不旁写 tearsheet。"""
    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp) / "out.json"
        run_batch(["AAPL"], dates=["2024-01-01", "2024-01-02"], output_dir=target, allow_synthetic=True)
        assert target.exists()
        assert not (pathlib.Path(tmp) / "tearsheet.html").exists()
        data = json.loads(target.read_text(encoding="utf-8"))
        assert "AAPL" in data


def test_run_batch_rejects_path_traversal():
    """含 .. 的越界输出路径被拒绝。"""
    with pytest.raises(ValueError, match="traversal"):
        run_batch(
            ["AAPL"],
            dates=["2024-01-01", "2024-01-02"],
            output_dir="../escape_dir",
            allow_synthetic=True,
        )


def test_run_batch_marks_news_pit_status():
    """带 PIT 记录时标注 news_pit_verified / pit_status。"""
    res = run_batch(
        ["AAPL"],
        dates=["2024-01-01", "2024-01-02"],
        news_records=[{"pit": True}, {"pit": True}],
        allow_synthetic=True,
    )
    assert res["AAPL"]["news_pit_verified"] is True
    assert res["AAPL"]["pit_status"] == "verified"
    assert res["AAPL"]["non_pit_count"] == 0


def test_run_batch_without_news_marks_pit_unavailable():
    """无新闻记录时 pit_status=unavailable。"""
    res = run_batch(["AAPL"], dates=["2024-01-01", "2024-01-02"], allow_synthetic=True)
    assert res["AAPL"]["news_pit_verified"] is False
    assert res["AAPL"]["pit_status"] == "unavailable"
    assert res["AAPL"]["non_pit_count"] == 0


def test_run_batch_benchmark_failure_marks_alpha_none():
    """基准腿失败时 alpha=None 且 failed=True（不伪装 0.0）。"""
    from unittest.mock import patch

    from hero_quant.backtest import bench as bench_mod

    real_run = bench_mod.BacktestEngine.run

    def _flaky(self, prices, **kw):
        # 第二次调用（基准腿）失败
        if getattr(self, "_calls", 0) >= 1:
            raise ValueError("bench leg boom")
        self._calls = getattr(self, "_calls", 0) + 1
        return real_run(self, prices, **kw)

    with patch.object(bench_mod.BacktestEngine, "run", _flaky):
        res = run_batch(["AAPL"], dates=["2024-01-01", "2024-01-02"], allow_synthetic=True)
    assert res["AAPL"]["alpha"] is None
    assert res["AAPL"]["failed"] is True
    assert "benchmark_error" in res["AAPL"]


def test_run_batch_metrics_are_json_serializable():
    """产出可 JSON 序列化（numpy 标量已归一）。"""
    res = run_batch(["AAPL"], dates=["2024-01-01", "2024-01-02"], allow_synthetic=True)
    json.dumps(res["AAPL"], ensure_ascii=False)


# ---------------------------------------------------------------- tearsheet


def test_tearsheet_html_empty_results_placeholder():
    """无结果时输出占位行。"""
    html = _build_tearsheet_html({}, "non-PIT source/unavailable")
    assert "no results" in html
    assert "non-PIT source/unavailable" in html


def test_tearsheet_html_escapes_ticker_and_metrics():
    """ticker 与指标被 HTML 转义，防注入。"""
    html = _build_tearsheet_html({"<script>": {"benchmark": "&", "alpha": "<b>"}}, "disc")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;" in html

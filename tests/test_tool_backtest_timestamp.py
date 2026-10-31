"""`run_backtest` 时间参数解析缺陷回归（缺陷 2 / 评测 BT-01）。

真实失败返回（2026-10-31 真实评测 BT-01）::

    {"ok": false,
     "error": "ValidationError: invalid timestamp: Unknown datetime string format, unable to parse: close"}

调用链：`tools/backtest.py:412`（`eng.run(..., **_eng_kw)`）→ `backtest/engine.py:646`
（`validate(prices, weights_on=eff_weights_on, price_date=pd_date)`）→
`backtest/validation.py:116`（`pd.Timestamp("close")` 失败被笼统包成 "invalid timestamp"）。

根因：模型把价格列名 `"close"` 当日期填进 `weights_on`，工具侧未做任何解析/校验就原样
下传引擎，错误串既不点名参数也没有稳定错误码，模型无法自纠。

本文件锁死修复后的契约：
- 非法时间参数（列名/乱串/NaT）→ `ok:false` + `error_code="invalid_timestamp_argument"` + 参数名；
- 合法调用仍然 `ok:true`（最小复现用例：修复前失败、修复后通过）；
- 空串等同于未提供（模型常用空串表示“没给”），但必须留痕而不是静默丢弃；
- 引擎/校验层同样点名参数并给稳定错误码（任何绕过工具层的调用方都受益）。
"""

from __future__ import annotations

import pandas as pd
import pytest

import hero_quant.tools.backtest as BT
from hero_quant.backtest.engine import BacktestEngine
from hero_quant.backtest.validation import ValidationError, validate

# 内存行情：无需联网，5 根 K 线足够引擎跑出 equity
_BARS = [
    {"date": "2024-01-02", "close": 100.0},
    {"date": "2024-01-03", "close": 101.0},
    {"date": "2024-01-04", "close": 99.5},
    {"date": "2024-01-05", "close": 102.0},
    {"date": "2024-01-08", "close": 103.0},
]


@pytest.fixture()
def offline_bars(monkeypatch):
    """把行情拉取替换成内存 bars，隔离网络（复现与回归都不依赖外部数据源）。"""
    monkeypatch.setattr(BT, "_fetch_bars_for_backtest", lambda *a, **k: (_BARS, None, None))


def _prices_df() -> pd.DataFrame:
    idx = pd.date_range("2024-01-02", periods=5, freq="D")
    return pd.DataFrame({"close": [100.0, 101.0, 99.5, 102.0, 103.0]}, index=idx)


# ---------------------------------------------------------------- 工具层：最小复现
def test_run_backtest_valid_weights_on_still_ok(offline_bars):
    """最小复现的另一半：合法 weights_on（真实日期）必须照常 ok:true。"""
    res = BT.run_backtest(
        symbol="600519.SH", start="2024-01-02", end="2024-01-08", weights=[1.0], weights_on="2024-01-02"
    )
    assert res["ok"] is True, res
    assert res["equity"], res
    assert res.get("error_code") is None


def test_run_backtest_rejects_column_name_as_weights_on(offline_bars):
    """复现 BT-01：weights_on="close"（价格列名）必须给清晰错误码，而非 pandas 原文。"""
    res = BT.run_backtest(
        symbol="600519.SH", start="2024-01-02", end="2024-01-08", weights=[1.0], weights_on="close"
    )
    assert res["ok"] is False
    assert res["error_code"] == "invalid_timestamp_argument"
    # 点名参数 + 可自纠提示（列名不是日期）
    assert "weights_on" in res["error"]
    assert "'close'" in res["error"]
    assert "column name" in res["error"]
    # 不再以 "ValidationError: invalid timestamp: ..." 的笼统形态抛给调用方
    assert not res["error"].startswith("ValidationError:")


def test_run_backtest_rejects_column_name_as_start(offline_bars):
    """start/end 同类参数：非法值不得静默回退到伪造窗口（旧码会落到 2026-08-01）。"""
    res = BT.run_backtest(symbol="600519.SH", start="close", end="2024-01-08", weights=[1.0])
    assert res["ok"] is False
    assert res["error_code"] == "invalid_timestamp_argument"
    assert "start" in res["error"]


def test_run_backtest_rejects_invalid_as_of(offline_bars):
    """as_of 非法值同样返回稳定错误码（不再有另一套 "invalid as_of" 文案）。"""
    res = BT.run_backtest(symbol="600519.SH", start="2024-01-02", end="2024-01-08", as_of="nope")
    assert res["ok"] is False
    assert res["error_code"] == "invalid_timestamp_argument"
    assert "as_of" in res["error"]


def test_run_backtest_blank_optional_time_args_treated_as_absent(offline_bars):
    """模型把可选参数填成空串时按“未提供”处理：合法调用仍 ok:true（留痕不静默）。"""
    res = BT.run_backtest(
        symbol="600519.SH",
        start="2024-01-02",
        end="2024-01-08",
        weights=[1.0],
        as_of="",
        weights_on="   ",
    )
    assert res["ok"] is True, res
    assert res["pit_basis"] == "inferred_as_of"


# ---------------------------------------------------------------- 校验/引擎层：点名参数
def test_validate_names_offending_argument_and_code():
    """validate 报错必须点名是哪个参数 + 稳定错误码（旧串只有 pandas 原文）。"""
    with pytest.raises(ValidationError) as ei:
        validate(_prices_df(), weights_on="close", price_date="2024-01-01")
    msg = str(ei.value)
    assert "invalid timestamp" in msg  # 既有测试 match 口径不回归
    assert "weights_on" in msg
    assert "invalid_timestamp_argument" in msg


def test_validate_rejects_nat_timestamp():
    """NaT 比较恒为 False，会让 PIT 校验静默失效 —— 必须 fail-closed。"""
    with pytest.raises(ValidationError, match="invalid_timestamp_argument"):
        validate(_prices_df(), weights_on=pd.NaT, price_date="2024-01-01")


def test_engine_run_surfaces_invalid_weights_on():
    """绕过工具层直接调引擎（server bundle / bench 路径）也拿到点名参数的错误。"""
    with pytest.raises(ValidationError) as ei:
        BacktestEngine().run(
            _prices_df(), weights=[1.0], weights_on="close", price_date="2024-01-05"
        )
    assert "weights_on" in str(ei.value)
    assert "invalid_timestamp_argument" in str(ei.value)

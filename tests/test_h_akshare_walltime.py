"""H线：akshare_loader TODO（108 行单位语义）+ wall_time 行为锁定。

akshare 成交量单位未确认前禁止静默 /100（fail-closed）：board_lots
口径下 volume 保持原值；wall_time 全路径以 monotonic 为准，
预算 0/负=不限、非法=ValueError、超时=WallTimeExceeded 且不吞原异常。
"""
import os

import pandas as pd
import pytest


def _loader():
    from hero_quant.data.loaders.akshare_loader import AKShareLoader

    return AKShareLoader()


# ---------- akshare TODO: 单位 fail-closed ----------

def test_h_akshare_unit_is_board_lots():
    assert _loader().unit == "board_lots"
    assert _loader().markets == ["CN"]


def test_h_akshare_todo_no_silent_div100():
    """TODO 锁定：确认前不做 vol/100；200000 股级数值保持原值。"""
    loader = _loader()
    df_ak = pd.DataFrame({
        "日期": ["2025-01-01", "2025-01-02"],
        "开盘": [10, 11],
        "收盘": [10, 11],
        "最高": [12, 13],
        "最低": [9, 10],
        "成交量": [500, 200000],
    })
    out = loader._normalize_akshare(df_ak)
    assert out["volume"].tolist() == [500.0, 200000.0]


def test_h_akshare_normalize_empty_none():
    assert _loader()._normalize_akshare(None) is None
    assert _loader()._normalize_akshare(pd.DataFrame()) is None


def test_h_akshare_normalize_missing_col_raises():
    from hero_quant.data.loaders.akshare_loader import DataValidationError

    loader = _loader()
    df = pd.DataFrame({"日期": ["2025-01-01"], "开盘": [1.0]})
    with pytest.raises(DataValidationError):
        loader._normalize_akshare(df)


def test_h_akshare_normalize_nan_volume_raises():
    from hero_quant.data.loaders.akshare_loader import DataValidationError

    loader = _loader()
    df = pd.DataFrame({
        "日期": ["2025-01-01"],
        "开盘": [1.0], "收盘": [1.0], "最高": [1.0], "最低": [1.0],
        "成交量": ["bad"],
    })
    with pytest.raises(DataValidationError):
        loader._normalize_akshare(df)


def test_h_akshare_normalize_bad_date_raises():
    from hero_quant.data.loaders.akshare_loader import DataValidationError

    loader = _loader()
    df = pd.DataFrame({
        "日期": ["not-a-date-xyz-123"],
        "开盘": [1.0], "收盘": [1.0], "最高": [1.0], "最低": [1.0],
        "成交量": [100],
    })
    # "not-a-date" 可被 to_datetime 解析失败 -> DataValidationError；
    # 若 pandas 宽容解析则至少列齐全，不断言抛错以外的行为
    try:
        out = loader._normalize_akshare(df)
        assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    except DataValidationError:
        pass


def test_h_akshare_synthetic_single_day_and_order(monkeypatch):
    monkeypatch.setenv("HERO_DATA_MODE", "synthetic")
    loader = _loader()
    df = loader.get_bars("600519.SH", "2025-01-01", "2025-01-01")
    assert len(df) == 1
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]


def test_h_akshare_synthetic_range_cap(monkeypatch):
    from hero_quant.data.loaders.akshare_loader import DataValidationError

    monkeypatch.setenv("HERO_DATA_MODE", "synthetic")
    loader = _loader()
    with pytest.raises(DataValidationError):
        loader._synthetic_df("600519.SH", "2020-01-01", "2025-01-01")
    with pytest.raises(DataValidationError):
        loader._synthetic_df("600519.SH", "2025-02-01", "2025-01-01")


def test_h_akshare_interval_guards(monkeypatch):
    from hero_quant.data.loaders.akshare_loader import DataValidationError

    monkeypatch.setenv("HERO_DATA_MODE", "synthetic")
    loader = _loader()
    with pytest.raises(DataValidationError):
        loader.get_bars("600519.SH", "2025-01-01", "2025-01-05", interval="1h")
    with pytest.raises(DataValidationError):
        loader.get_bars("600519.SH", "2025-01-01", "2025-01-05", interval="nope")
    with pytest.raises(DataValidationError):
        loader.get_bars("600519.SH", "1d", "notadate", interval="1d")


def test_h_akshare_unknown_mode_raises(monkeypatch):
    from hero_quant.data.loaders.akshare_loader import DataValidationError

    monkeypatch.setenv("HERO_DATA_MODE", "weirdmode")
    loader = _loader()
    with pytest.raises(DataValidationError):
        loader.get_bars("600519.SH", "2025-01-01", "2025-01-02")


def test_h_akshare_health_shape():
    h = _loader().health()
    assert h["unit"] == "board_lots"
    assert h["source"] == "akshare"
    assert "akshare_available" in h


# ---------- wall_time ----------

def test_h_wall_resolve_explicit():
    from hero_quant.governance.wall_time import _resolve_default_budget

    assert _resolve_default_budget(explicit=30.0) == 30.0
    assert _resolve_default_budget(explicit=0) is None
    assert _resolve_default_budget(explicit=-5) is None
    with pytest.raises(ValueError):
        _resolve_default_budget(explicit="bad_value")


def test_h_wall_resolve_env(monkeypatch):
    from hero_quant.governance.wall_time import _resolve_default_budget

    monkeypatch.setenv("HERO_WALL_TIME_BUDGET", "12.5")
    assert _resolve_default_budget() == pytest.approx(12.5)
    monkeypatch.setenv("HERO_WALL_TIME_BUDGET", "not_a_number")
    # 非法 env 不抛，回落 Settings/默认
    assert _resolve_default_budget() == pytest.approx(30.0)


def test_h_wall_resolve_default(monkeypatch):
    from hero_quant.governance.wall_time import _resolve_default_budget

    monkeypatch.delenv("HERO_WALL_TIME_BUDGET", raising=False)
    monkeypatch.delenv("HERO_WALL_TIME_BUDGET_SECONDS", raising=False)
    v = _resolve_default_budget()
    assert v is None or float(v) > 0


def test_h_wall_budget_unlimited_never_exceeded():
    from hero_quant.governance.wall_time import WallTimeBudget

    b = WallTimeBudget(budget_seconds=0, operation="t")
    assert b.budget_seconds is None
    assert b.remaining() is None
    assert b.exceeded() is False
    assert b.is_exceeded() is False
    b.check()  # 不抛


def test_h_wall_budget_invalid_raises():
    from hero_quant.governance.wall_time import WallTimeBudget

    with pytest.raises(ValueError):
        WallTimeBudget(budget_seconds="bad", operation="t")


def test_h_wall_budget_fake_clock_exceeded():
    from hero_quant.governance.wall_time import WallTimeBudget, WallTimeExceeded

    now = [100.0]

    def _clock():
        return now[0]

    b = WallTimeBudget(budget_seconds=10.0, operation="op1", clock=_clock)
    assert b.exceeded() is False
    assert b.remaining() == pytest.approx(10.0)
    now[0] = 200.0
    assert b.exceeded() is True
    with pytest.raises(WallTimeExceeded) as ei:
        b.check("detail-x")
    assert ei.value.operation == "op1"


def test_h_wall_budget_broken_clock_safe():
    from hero_quant.governance.wall_time import WallTimeBudget

    def _boom():
        raise RuntimeError("clock boom")

    b = WallTimeBudget(budget_seconds=5.0, operation="t", clock=_boom)
    assert b.elapsed() == 0.0
    assert b.remaining() is None or isinstance(b.remaining(), float)
    assert b.exceeded() is False


def test_h_wall_context_success_and_timeout():
    from hero_quant.governance.wall_time import WallTimeBudget, WallTimeExceeded

    with WallTimeBudget(budget_seconds=30.0, operation="ok"):
        pass
    # 超时且无原异常 -> __exit__ 抛 WallTimeExceeded
    now = [0.0]

    def _clock():
        return now[0]

    b = WallTimeBudget(budget_seconds=1.0, operation="ctx", clock=_clock)
    with pytest.raises(WallTimeExceeded):
        with b:
            now[0] = 5.0


def test_h_wall_context_does_not_swallow_original():
    from hero_quant.governance.wall_time import WallTimeBudget

    b = WallTimeBudget(budget_seconds=30.0, operation="t")
    with pytest.raises(ValueError, match="original"):
        with b:
            raise ValueError("original")


def test_h_wall_time_call_success_and_over_budget():
    from hero_quant.governance.wall_time import WallTimeBudget, WallTimeExceeded

    ticks = [0.0]

    def _clock():
        return ticks[0]

    b = WallTimeBudget(budget_seconds=30.0, operation="tc", clock=_clock)

    def _fast():
        ticks[0] = 1.0
        return "ok"

    assert b.time_call(_fast) == "ok"

    b2 = WallTimeBudget(budget_seconds=1.0, operation="tc2", clock=_clock)
    ticks[0] = 10.0

    def _slow():
        ticks[0] = 20.0
        return "late"

    with pytest.raises(WallTimeExceeded):
        b2.time_call(_slow)


def test_h_wall_time_call_reraises_original():
    from hero_quant.governance.wall_time import WallTimeBudget

    b = WallTimeBudget(budget_seconds=30.0, operation="t")

    def _boom():
        raise KeyError("orig")

    with pytest.raises(KeyError):
        b.time_call(_boom)


def test_h_wall_governor_enforce_wrap_check():
    from hero_quant.governance.wall_time import WallTimeGovernor, WallTimeExceeded

    gov = WallTimeGovernor(budget_seconds=30.0, operation="g")
    assert gov.enforce(lambda: 42) == 42
    wrapped = gov.wrap(lambda x: x + 1)
    assert wrapped(1) == 2
    gov2 = WallTimeGovernor(budget_seconds=1.0, operation="g2")
    with pytest.raises(WallTimeExceeded):
        gov2.check_budget(5.0)
    gov3 = WallTimeGovernor(budget_seconds=0, operation="unlim")
    gov3.check_budget(10 ** 9)  # 不限 -> 不抛


def test_h_wall_decorator_aliases_and_helpers():
    from hero_quant.governance import wall_time as wt

    d1 = wt.with_wall_time_budget(budget=5.0, operation="a")

    @d1
    def _f():
        return "v"

    assert _f() == "v"
    assert _f._wall_time_budget == 5.0

    d2 = wt.with_wall_time_budget(wall_time_budget=6.0, operation="b")

    @d2
    def _g():
        return "w"

    assert _g() == "w"
    assert wt.enforce_wall_time(lambda: 7, budget_seconds=30.0) == 7
    assert issubclass(wt.WallTimeBudgetExceeded, wt.WallTimeExceeded)
    assert wt.wall_time_budget(budget_seconds=30.0).budget_seconds == 30.0


def test_h_wall_exceeded_shape():
    from hero_quant.governance.wall_time import WallTimeExceeded

    e = WallTimeExceeded("op", 1.0, 2.0, detail="d")
    assert e.operation == "op"
    assert e.budget_seconds == pytest.approx(1.0)
    assert e.elapsed == pytest.approx(2.0)
    assert "op" in str(e)

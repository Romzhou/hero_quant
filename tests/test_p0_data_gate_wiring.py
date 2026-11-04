"""P0-2 数据三道闸接线：registry 质量门 + 工具层 fail-closed + PIT 显式标注。

三道闸（AGENTS §11「接线可达」L2 证据）：

1. MarketDataRegistry.get_bars 日线成功返回**前**跑 data.quality.check_bars；
   block 级 finding 直接抛 DataQualityError（fail-closed），warn 只留痕不阻断。
2. tools.market_data.get_market_data / get_bars_range 捕获 DataQualityError
   后**不得**落入 synthetic 兜底：DataQualityError 是 RuntimeError 子类，会被
   「瞬时错误 -> 合成回退」分支吞掉，坏数据于是被假 live 掩盖。
3. PIT 未接线必须显式标注 pit.applied=False，不得让下游默认「已在用 PIT 数据」。

隔离方式：每个用例自建 MarketDataRegistry + 假 loader（markets/unit/get_bars/health，
满足 trait.validate_loader 契约），不碰网络、不共享 registry 缓存。
"""

from __future__ import annotations

import pytest

from hero_quant.data.quality import DataQualityError
from hero_quant.data.registry import MarketDataRegistry, clear_settings_cache

# 2024-01-02..01-05 全为工作日，避免周末规则与「乱序/坏值」用例互相干扰。
_WEEK = ("2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05")


def _bars(dates, closes, volume=100.0):
    """构造 loader 级 bars（与真实 loader 同形：date 键 + OHLCV）。"""
    return [
        {"date": d, "open": c, "close": c, "high": c + 1.0, "low": max(c - 1.0, 0.01), "volume": volume}
        for d, c in zip(dates, closes)
    ]


class _FakeLoader:
    """最小 loader 契约：markets/unit/get_bars/health。

    name/source 必须在 VALID_SOURCES 白名单内，否则 register 直接拒绝。
    """

    def __init__(self, bars, *, markets=("CN",), unit="board_lots", source="tencent", name="tencent"):
        self._bars = bars
        self.markets = list(markets)
        self.unit = unit
        self.source = source
        self.name = name

    def get_bars(self, symbol, start, end, interval="1d"):
        return self._bars

    def health(self):
        return {"status": "ok"}


def _registry(bars, **kwargs):
    reg = MarketDataRegistry()
    reg.register(_FakeLoader(bars, **kwargs))
    return reg


@pytest.fixture(autouse=True)
def _live_mode(monkeypatch):
    """默认 live：否则 _resolve_provenance 把一切判成 synthetic，质量门全被跳过。"""
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    clear_settings_cache()
    yield
    clear_settings_cache()


# ---------------------------------------------------------------- 闸1：负价格阻断


def test_daily_negative_close_blocks_get_bars():
    """负价格日线必须在 registry 出口被拦（此前只有未接线的 snapshot 路径会拦）。"""
    reg = _registry(_bars(_WEEK, [-1.0, -1.1, -1.2, -1.3]))
    with pytest.raises(DataQualityError) as excinfo:
        reg.get_bars("600519.SH", _WEEK[0], _WEEK[-1])
    assert excinfo.value.error == "data_quality_error"
    assert excinfo.value.reason == "price_not_positive"
    assert "price_not_positive" in excinfo.value.codes
    assert "rows=4" in excinfo.value.detail, f"detail 应有界且带计数，实际 {excinfo.value.detail!r}"


def test_warn_level_finding_does_not_block():
    """warn 不阻断：区间成交量恒为 0（停牌/坏源）只留痕，数据照旧可用。"""
    reg = _registry(_bars(_WEEK, [10.0, 10.1, 10.2, 10.3], volume=0))
    bars, prov = reg.get_bars("600519.SH", _WEEK[0], _WEEK[-1])
    assert len(bars) == 4
    assert prov.extra["quality"]["ok"] is True
    assert prov.extra["quality"]["reason"] is None


# ------------------------------------------------- 闸2：工具层不得用合成掩盖坏数据


def test_tool_layer_does_not_mask_quality_error_with_synthetic(monkeypatch):
    """get_market_data 必须把质量错翻成结构化失败，禁止退回合成序列冒充行情。"""
    import hero_quant.tools.market_data as md

    reg = _registry(_bars(_WEEK, [-1.0, -1.1, -1.2, -1.3]))
    monkeypatch.setattr(md, "_get_shared_registry", lambda: reg)

    out = md.get_market_data("600519.SH", start=_WEEK[0], end=_WEEK[-1], allow_synthetic=True)
    assert out["ok"] is False
    assert out["error"] == "data_quality_error"
    assert out["reason"] == "price_not_positive"
    assert out["detail"], "必须带 reason 明细，便于排查而不是只给一句 failed"
    assert out["bars"] == [], "坏数据不得被合成 bars 替换后返回"
    assert (out.get("provenance") or {}).get("source") != "synthetic"


def test_tool_batch_does_not_mask_quality_error_per_symbol(monkeypatch):
    """批量工具逐标的分级：坏标的与好标的不混成一坨，且坏标的不得合成兜底。"""
    import hero_quant.tools.market_data as md

    reg = _registry(_bars(_WEEK, [-1.0, -1.1, -1.2, -1.3]))
    monkeypatch.setattr(md, "_get_shared_registry", lambda: reg)

    batch = md.get_bars_range(["600519.SH"], start=_WEEK[0], end=_WEEK[-1], allow_synthetic=True)
    entry = batch["data"]["600519.SH"]
    assert batch["ok"] is False
    assert entry["ok"] is False
    assert entry["error"] == "data_quality_error"
    assert entry["reason"] == "price_not_positive"
    assert entry["bars"] == []
    assert (entry.get("provenance") or {}).get("source") != "synthetic"


# ------------------------------------------------------- 闸3：PIT 显式标注 + 摘要透传


def test_clean_daily_bars_pass_with_bounded_quality_summary():
    """正常日线通过，摘要只带有界字段（不塞 findings 全表）。"""
    reg = _registry(_bars(_WEEK, [10.0, 10.1, 10.2, 10.3]))
    bars, prov = reg.get_bars("600519.SH", _WEEK[0], _WEEK[-1])

    assert len(bars) == 4
    summary = prov.extra["quality"]
    assert summary == {"ok": True, "reason": None, "block_codes": [], "checked_rows": 4}
    assert "findings" not in summary


def test_tool_marks_pit_not_applied_and_passes_quality_through(monkeypatch):
    """PIT 未接线必须 applied=False；质量摘要经 provenance 透传到工具层。"""
    import hero_quant.tools.market_data as md

    reg = _registry(_bars(_WEEK, [10.0, 10.1, 10.2, 10.3]))
    monkeypatch.setattr(md, "_get_shared_registry", lambda: reg)

    out = md.get_market_data("600519.SH", start=_WEEK[0], end=_WEEK[-1])
    assert out["ok"] is True
    assert out["pit"] == {"applied": False, "reason": "registry_get_bars"}
    assert out["provenance"]["extra"]["quality"]["ok"] is True

    batch = md.get_bars_range(["600519.SH"], start=_WEEK[0], end=_WEEK[-1])
    assert batch["data"]["600519.SH"]["pit"] == {"applied": False, "reason": "registry_get_bars"}


# ---------------------------------------------------------- 真实坏数据：量级混币种


def test_mixed_price_scale_series_blocks_as_return_outlier():
    """同序列混币种/量级（10 -> 7000）：单日涨幅远超阈值，必须拒绝而不是当正常行情。"""
    reg = _registry(_bars(_WEEK, [10.0, 10.2, 7000.0, 7010.0]))
    with pytest.raises(DataQualityError) as excinfo:
        reg.get_bars("600519.SH", _WEEK[0], _WEEK[-1])
    assert excinfo.value.reason == "return_outlier"
    assert "return_outlier" in excinfo.value.codes


def test_unsorted_but_valid_bars_not_false_blocked():
    """乱序本身不是坏数据：quality 按日期排序后判定，不得误报涨跌幅越界。"""
    shuffled = [_bars(_WEEK, [10.0, 10.1, 10.2, 10.3])[i] for i in (2, 0, 3, 1)]
    reg = _registry(shuffled)
    bars, prov = reg.get_bars("600519.SH", _WEEK[0], _WEEK[-1])
    assert len(bars) == 4
    assert prov.extra["quality"]["ok"] is True


# ------------------------------------------------- 显式跳过口径（禁止静默跳过）


def test_synthetic_source_marked_skip_not_blocked(monkeypatch):
    """synthetic 逐日历日铺满（含周末/占位）：显式跳过质量门，但必须留痕。"""
    monkeypatch.setenv("HERO_DATA_MODE", "synthetic")
    clear_settings_cache()

    reg = _registry(_bars(("2024-01-05", "2024-01-06", "2024-01-07"), [-1.0, 1499.0, 1500.0], volume=0))
    bars, prov = reg.get_bars("600519.SH", "2024-01-05", "2024-01-07")

    assert len(bars) == 3
    assert prov.source == "synthetic"
    assert prov.extra["quality_skipped"] == "synthetic"
    assert "quality" not in prov.extra, "跳过的批次不得留一个看起来通过的质量摘要"


def test_intraday_interval_marked_skip_not_blocked():
    """分钟线不套日线规则（涨跌幅/周末口径不同）：显式标注 interval 而非静默放行。"""
    reg = _registry(_bars(("2024-01-02", "2024-01-03"), [-1.0, -2.0]))
    bars, prov = reg.get_bars("600519.SH", "2024-01-02", "2024-01-03", interval="5m")

    assert len(bars) == 2
    assert prov.extra["quality_skipped"] == "interval:5m"
    assert "quality" not in prov.extra


def test_bars_without_trade_date_marked_skip():
    """整批取不到日期：日期类规则无从判定，显式标注跳过。"""
    reg = _registry([{"close": 10.0, "volume": 100}])
    _bars_out, prov = reg.get_bars("600519.SH", _WEEK[0], _WEEK[1])
    assert prov.extra["quality_skipped"] == "no_trade_date"


def test_dataframe_bars_projected_from_datetime_index():
    """DataFrame 形态（DatetimeIndex 无日期列）也要能投影进质量门，且坏值照样被拦。"""
    import pandas as pd

    idx = pd.to_datetime(list(_WEEK))
    good = pd.DataFrame(
        {
            "open": [10.0, 10.1, 10.2, 10.3],
            "close": [10.0, 10.1, 10.2, 10.3],
            "high": [11.0, 11.1, 11.2, 11.3],
            "low": [9.0, 9.1, 9.2, 9.3],
            "volume": [100.0, 100.0, 100.0, 100.0],
        },
        index=idx,
    )
    bars, prov = _registry(good).get_bars("600519.SH", _WEEK[0], _WEEK[-1])
    assert len(bars) == 4
    assert prov.extra["quality"] == {"ok": True, "reason": None, "block_codes": [], "checked_rows": 4}

    bad = good.copy()
    bad.loc[idx[0], "close"] = -1.0
    with pytest.raises(DataQualityError) as excinfo:
        _registry(bad).get_bars("600519.SH", _WEEK[0], _WEEK[-1])
    assert excinfo.value.reason == "price_not_positive"


def test_weekend_rule_is_cn_only_not_false_positive_on_us():
    """周末规则是 A 股专属：US/CRYPTO 无交易所日历套用会误杀，CN 仍 fail-closed。

    非 CN 只把 A 股专属 block 降级为 warn，并把降级事实写进 provenance（不静默）。
    """
    weekend = ("2024-01-05", "2024-01-06")  # 2024-01-06 为周六

    cn = _registry(_bars(weekend, [10.0, 10.1]))
    with pytest.raises(DataQualityError) as excinfo:
        cn.get_bars("600519.SH", weekend[0], weekend[1])
    assert excinfo.value.reason == "weekend_bar"

    us = _registry(_bars(weekend, [10.0, 10.1]), markets=("US",), unit="shares", source="yahoo", name="yahoo")
    bars, prov = us.get_bars("AAPL.US", weekend[0], weekend[1])
    assert len(bars) == 2
    assert prov.extra["quality"]["ok"] is True
    assert prov.extra["quality_relaxed"] == ["weekend_bar"]


def test_missing_volume_schema_still_checks_prices():
    """源不提供 volume 时：volume 维度显式标注，但价格维度必须照旧拦截。

    防止「删掉 volume 字段就能绕过质量门」的旁路。
    """
    reg_bad = _registry([{"date": d, "close": -1.0} for d in _WEEK])
    with pytest.raises(DataQualityError) as excinfo:
        reg_bad.get_bars("600519.SH", _WEEK[0], _WEEK[-1])
    assert excinfo.value.reason == "price_not_positive"

    reg_ok = _registry([{"date": d, "close": c} for d, c in zip(_WEEK, [10.0, 10.1, 10.2, 10.3])])
    bars, prov = reg_ok.get_bars("600519.SH", _WEEK[0], _WEEK[-1])
    assert len(bars) == 4
    assert prov.extra["quality"]["ok"] is True
    assert prov.extra["quality_volume"] == "absent"

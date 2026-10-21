"""W4 覆盖率补齐：agent/grounding.py 未覆盖分支。

覆盖目标（src/hero_quant/agent/grounding.py）：
- _normalize_price_value：bool 拒绝、货币符号/千分位/空格/百分号剥离、非法串拒绝
- _validate_provenance：None / 非 dict / source 非 str / source 空串 / unit 非法
- _normalize_price_strict：bool / NaN / Inf 拒绝
- ingest：bar 非 dict 拒绝、close 缺失、low/high 回落 close、空 bars
- assert_price：authorized 的 set/list/tuple/dict 形态、边界价命中、非数值价格拒绝
- render_block：空账本表头、带日期与不带日期两种行

断言均为具体数值/异常类型/字符串片段，不使用弱断言。
"""

from __future__ import annotations

import math

import pytest

from hero_quant.agent.grounding import (
    GroundingError,
    GroundingLedger,
    _normalize_price_strict,
    _normalize_price_value,
    _validate_provenance,
)

_PROV = {"source": "test", "unit": "shares"}


# ---------------------------------------------------------------- 价格归一


def test_normalize_price_value_rejects_bool():
    """bool 不被当作价格（True==1 会造成误判）。"""
    with pytest.raises(ValueError, match="invalid price value"):
        _normalize_price_value(True)


def test_normalize_price_value_accepts_numbers():
    """int/float 直接归一为 float。"""
    assert _normalize_price_value(1500) == 1500.0
    assert _normalize_price_value(1500.5) == 1500.5


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1,500", 1500.0),
        ("$1,500", 1500.0),
        ("¥1,400", 1400.0),
        ("￥99", 99.0),
        ("€10.5", 10.5),
        ("£7", 7.0),
        ("1 500", 1500.0),
        ("12%", 12.0),
        ("-3.5", -3.5),
        ("+42", 42.0),
    ],
)
def test_normalize_price_value_strips_formatting(raw, expected):
    """货币符号/千分位/空格/百分号被剥离后转 float。"""
    assert _normalize_price_value(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["", "abc", "1.2.3", "--5"])
def test_normalize_price_value_rejects_invalid(raw):
    """非法价格串抛 ValueError。"""
    with pytest.raises(ValueError, match="invalid price value"):
        _normalize_price_value(raw)


def test_normalize_price_strict_rejects_bool():
    """strict 归一对 bool 直接抛 GroundingError。"""
    with pytest.raises(GroundingError, match="bool not allowed"):
        _normalize_price_strict(True)


def test_normalize_price_strict_rejects_nan_and_inf():
    """NaN / Inf 价格被拒（非有限值不得进证据）。"""
    with pytest.raises(GroundingError, match="non-finite"):
        _normalize_price_strict(float("nan"))
    with pytest.raises(GroundingError, match="non-finite"):
        _normalize_price_strict(float("inf"))


# ---------------------------------------------------------------- provenance 契约


def test_validate_provenance_requires_mapping():
    """provenance 缺失或非 dict 均被拒。"""
    with pytest.raises(GroundingError, match="missing provenance"):
        _validate_provenance(None)
    with pytest.raises(GroundingError, match="provenance must be dict"):
        _validate_provenance("source=x")


def test_validate_provenance_requires_non_empty_source():
    """source 必须是非空字符串。"""
    with pytest.raises(GroundingError, match="provenance.source"):
        _validate_provenance({"source": "", "unit": "shares"})
    with pytest.raises(GroundingError, match="provenance.source"):
        _validate_provenance({"source": 123, "unit": "shares"})


def test_validate_provenance_unit_whitelist():
    """unit 必须在 board_lots/shares 白名单内。"""
    with pytest.raises(GroundingError, match="provenance.unit"):
        _validate_provenance({"source": "x", "unit": "hand"})
    assert _validate_provenance({"source": " x ", "unit": "board_lots"}) == {"source": "x", "unit": "board_lots"}


# ---------------------------------------------------------------- ingest


def test_ingest_rejects_non_dict_bar():
    """bar 非 dict 拒绝（防脏数据进证据）。"""
    g = GroundingLedger()
    with pytest.raises(GroundingError, match="bar must be dict"):
        g.ingest("AAPL", [123.0], provenance=_PROV)


def test_ingest_low_high_fall_back_to_close():
    """low/high 缺失时回落为 close。"""
    g = GroundingLedger()
    g.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    ev = g._evidence["AAPL"]
    assert ev["low"] == 100.0
    assert ev["high"] == 100.0
    assert ev["bars"][0]["low"] == 100.0
    assert ev["bars"][0]["high"] == 100.0


def test_ingest_without_close_keeps_none():
    """close 缺失时 close 归一为 None（不臆造 0）。"""
    g = GroundingLedger()
    g.ingest("AAPL", [{"date": "2026-01-01"}], provenance=_PROV)
    ev = g._evidence["AAPL"]
    assert ev["bars"][0]["close"] is None
    assert ev["closes"] == set()


def test_ingest_aggregates_low_high_extremes():
    """low/high 取全 bars 的最小/最大值。"""
    g = GroundingLedger()
    g.ingest(
        "AAPL",
        [{"close": 100.0, "low": 90.0, "high": 110.0}, {"close": 105.0, "low": 80.0, "high": 130.0}],
        provenance=_PROV,
    )
    ev = g._evidence["AAPL"]
    assert ev["low"] == 80.0
    assert ev["high"] == 130.0
    assert ev["closes"] == {100.0, 105.0}


# ---------------------------------------------------------------- assert_price


def test_assert_price_accepts_all_authorized_container_types():
    """authorized 支持 set/frozenset/list/tuple/dict。"""
    g = GroundingLedger()
    g.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    for auth in ({"AAPL"}, frozenset({"AAPL"}), ["AAPL"], ("AAPL",), {"AAPL": 1}):
        g.assert_price("AAPL", 100.0, authorized=auth)


def test_assert_price_rejects_symbol_outside_authorized_dict():
    """authorized dict 不含该 symbol 时拒绝。"""
    g = GroundingLedger()
    g.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    with pytest.raises(GroundingError, match="not in authorized snapshot"):
        g.assert_price("TSLA", 100.0, authorized={"AAPL": 1})


def test_assert_price_rejects_symbol_outside_authorized_set():
    """authorized 集合不含该 symbol 时拒绝。"""
    g = GroundingLedger()
    g.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    with pytest.raises(GroundingError, match="not in authorized snapshot"):
        g.assert_price("TSLA", 100.0, authorized={"AAPL"})


def test_assert_price_accepts_boundary_prices():
    """价格等于 low / high 边界时通过（区间含端点）。"""
    g = GroundingLedger()
    g.ingest("AAPL", [{"close": 100.0, "low": 90.0, "high": 110.0}], provenance=_PROV)
    g.assert_price("AAPL", 90.0)
    g.assert_price("AAPL", 110.0)


def test_assert_price_rejects_non_numeric_price():
    """非数值价格抛 GroundingError（不静默放行）。"""
    g = GroundingLedger()
    g.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    with pytest.raises(GroundingError, match="invalid price value"):
        g.assert_price("AAPL", "not-a-number")


def test_assert_price_rejects_unknown_symbol_with_authorized():
    """authorized 命中但账本无该 symbol 时仍拒绝（证据缺失）。"""
    g = GroundingLedger()
    g.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    with pytest.raises(GroundingError, match="unknown symbol"):
        g.assert_price("NOPE", 100.0, authorized={"NOPE"})


# ---------------------------------------------------------------- render_block


def test_render_block_empty_ledger_returns_header_only():
    """空账本渲染为表头，保证 prompt 合法。"""
    assert GroundingLedger().render_block() == "## Ground Truth\n"


def test_render_block_includes_date_when_present():
    """带 date 的 bar 渲染出日期。"""
    g = GroundingLedger()
    g.ingest("AAPL", [{"close": 100.0, "date": "2026-01-02"}], provenance=_PROV)
    block = g.render_block()
    assert "AAPL: close 100.0 on 2026-01-02" in block


def test_render_block_omits_date_when_absent():
    """无 date 的 bar 不输出 " on " 片段。"""
    g = GroundingLedger()
    g.ingest("AAPL", [{"close": 100.0}], provenance=_PROV)
    block = g.render_block()
    assert "AAPL: close 100.0" in block
    assert " on " not in block


def test_render_block_uses_normalized_numbers_only():
    """渲染只用归一化数值，不回显原始字符串。"""
    g = GroundingLedger()
    g.ingest("AAPL", [{"close": "1,500", "low": "$1,400", "high": "¥1,600"}], provenance=_PROV)
    block = g.render_block()
    assert "1,500" not in block
    assert "$" not in block
    assert "1500.0" in block


def test_grounding_low_high_are_finite():
    """证据区间为有限值（NaN/Inf 不得入账）。"""
    g = GroundingLedger()
    g.ingest("AAPL", [{"close": 100.0, "low": 90.0, "high": 110.0}], provenance=_PROV)
    ev = g._evidence["AAPL"]
    assert math.isfinite(ev["low"]) and math.isfinite(ev["high"])

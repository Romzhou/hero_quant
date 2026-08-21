"""A1-1: safe_ticker_component — 非法 ticker 拒绝 (TDD)."""

from __future__ import annotations

import pytest

from hero_quant.security.sanitize import safe_ticker_component


def test_rejects_traversal():
    with pytest.raises(ValueError):
        safe_ticker_component("../../../etc/passwd")


def test_accept_valid():
    assert safe_ticker_component("600519.SS") == "600519.SS"


def test_reject_dots_only():
    with pytest.raises(ValueError):
        safe_ticker_component("...")
    with pytest.raises(ValueError):
        safe_ticker_component(".")
    with pytest.raises(ValueError):
        safe_ticker_component("..")


def test_reject_empty():
    with pytest.raises(ValueError):
        safe_ticker_component("")
    with pytest.raises(ValueError):
        safe_ticker_component("   ")


def test_reject_too_long():
    with pytest.raises(ValueError):
        safe_ticker_component("A" * 33)
    # boundary: 32 is ok
    assert safe_ticker_component("A" * 32) == "A" * 32


def test_reject_invalid_chars():
    with pytest.raises(ValueError):
        safe_ticker_component("A/B")
    with pytest.raises(ValueError):
        safe_ticker_component("ticker\n")
    with pytest.raises(ValueError):
        safe_ticker_component("a;rm")

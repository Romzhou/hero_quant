"""PR-112 定价层基础契约：默认阀门关闭、未知 backend 显式失败、价卡不可变。"""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from hero_quant.pricing.base import (
    NullPricingProvider,
    PriceCard,
    PricingProvider,
    get_pricing_provider,
)


def test_default_provider_is_null_valve_closed():
    provider = get_pricing_provider()

    assert isinstance(provider, NullPricingProvider)
    assert isinstance(provider, PricingProvider)
    assert provider.backend == "null"
    assert get_pricing_provider("").backend == "null"


def test_null_provider_never_returns_price_or_cards():
    provider = NullPricingProvider()

    assert provider.price("gpt-4o-mini") is None
    assert provider.price("gpt-4o-mini", at=date(2030, 1, 1)) is None
    assert provider.list_cards() == []
    assert provider.list_cards("gpt-4o-mini") == []


def test_unknown_backend_fails_loudly():
    with pytest.raises(ValueError, match="unknown pricing backend"):
        get_pricing_provider("some-paid-vendor")


def test_price_card_is_frozen_and_defaults_to_usd():
    card = PriceCard(
        model_id="m",
        input_usd_per_mtok=1.0,
        output_usd_per_mtok=2.0,
        effective_from=date(2025, 1, 1),
        source="test",
    )

    assert card.currency == "USD"
    with pytest.raises(dataclasses.FrozenInstanceError):
        card.input_usd_per_mtok = 9.9  # type: ignore[misc]

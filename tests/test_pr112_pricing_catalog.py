"""PR-112 目录定价源：命中 / 未命中 / effective_from 时点选择。"""

from __future__ import annotations

from datetime import date

from hero_quant.llm.catalog import MODEL_CATALOG
from hero_quant.pricing.base import get_pricing_provider
from hero_quant.pricing.catalog_provider import SOURCE, CatalogPricingProvider


def test_catalog_backend_is_selectable():
    provider = get_pricing_provider("catalog")

    assert isinstance(provider, CatalogPricingProvider)
    assert provider.backend == "catalog"


def test_catalog_price_matches_model_info():
    provider = CatalogPricingProvider()
    info = MODEL_CATALOG["gpt-4o-mini"]

    card = provider.price("gpt-4o-mini", at=info.effective_from)

    assert card is not None
    assert card.model_id == info.name
    assert card.input_usd_per_mtok == info.input_price_per_million_tokens
    assert card.output_usd_per_mtok == info.output_price_per_million_tokens
    assert card.effective_from == info.effective_from
    assert card.source == SOURCE
    assert card.currency == "USD"


def test_catalog_price_unknown_model_is_none_not_default():
    provider = CatalogPricingProvider()

    assert provider.price("acme-super-model") is None
    assert provider.price("") is None
    assert provider.price(None) is None  # type: ignore[arg-type]


def test_catalog_price_respects_effective_from_point_in_time():
    provider = CatalogPricingProvider()
    info = MODEL_CATALOG["gpt-4o-mini"]
    day_before = date.fromordinal(info.effective_from.toordinal() - 1)

    assert provider.price("gpt-4o-mini", at=day_before) is None, "早于生效日不得拿今天的价充数"
    assert provider.price("gpt-4o-mini", at=info.effective_from) is not None


def test_catalog_price_matches_versioned_suffix():
    provider = CatalogPricingProvider()
    info = MODEL_CATALOG["deepseek-chat"]

    card = provider.price("deepseek-chat-2026-01-01", at=info.effective_from)

    assert card is not None and card.model_id == info.name


def test_catalog_list_cards_enumeration_is_deterministic():
    provider = CatalogPricingProvider()

    cards = provider.list_cards()
    assert [card.model_id for card in cards] == sorted(MODEL_CATALOG)
    assert provider.list_cards("gpt-4o-mini") == [provider.price("gpt-4o-mini")]
    assert provider.list_cards("acme-super-model") == []


def test_catalog_cards_never_zero_priced():
    for card in CatalogPricingProvider().list_cards():
        assert card.input_usd_per_mtok > 0 and card.output_usd_per_mtok > 0

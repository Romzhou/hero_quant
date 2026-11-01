"""基于 llm/catalog.py 的定价源（W4 · PR-112）。

目录价是「当前价」而非版本化历史价：本 provider 只做一件事——把价卡按
effective_from 做时点选择。早于生效日的查询返回 None（不拿今天的价冒充历史价），
未知模型同样返回 None，由调用方记 unpriced。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Mapping

from ..llm.catalog import MODEL_CATALOG, ModelInfo
from .base import PriceCard, PricingProvider

SOURCE = "llm.catalog"
"""价卡来源标记：目录价无版本号，不承诺可回溯到 effective_from 之前的真实账单。"""


def _resolve(catalog: Mapping[str, ModelInfo], model_id: str | None) -> ModelInfo | None:
    """按模型名取目录条目；兼容版本化后缀（-日期 / @v2 / :latest）。"""
    name = str(model_id or "").strip()
    if not name:
        return None
    info = catalog.get(name)
    if info is not None:
        return info
    for known in sorted(catalog, key=len, reverse=True):
        if name.startswith(known + "-") or name.startswith(known + "@") or name.startswith(known + ":"):
            return catalog[known]
    return None


def _card(info: ModelInfo) -> PriceCard:
    return PriceCard(
        model_id=info.name,
        input_usd_per_mtok=float(info.input_price_per_million_tokens),
        output_usd_per_mtok=float(info.output_price_per_million_tokens),
        effective_from=info.effective_from,
        source=SOURCE,
    )


class CatalogPricingProvider(PricingProvider):
    """目录定价源：单卡按时点过滤，无历史版本链。"""

    backend = "catalog"

    def __init__(self, catalog: Mapping[str, ModelInfo] | None = None) -> None:
        self._catalog: Mapping[str, ModelInfo] = MODEL_CATALOG if catalog is None else catalog

    def price(self, model_id: str, *, at: date | None = None) -> PriceCard | None:
        info = _resolve(self._catalog, model_id)
        if info is None:
            return None
        day = at if at is not None else date.today()
        if isinstance(day, datetime):  # 调用方可能直接传 datetime，取日期部分避免比较报错
            day = day.date()
        if info.effective_from > day:
            return None
        return _card(info)

    def list_cards(self, model_id: str | None = None) -> list[PriceCard]:
        if model_id is not None:
            info = _resolve(self._catalog, model_id)
            return [] if info is None else [_card(info)]
        return [_card(self._catalog[name]) for name in sorted(self._catalog)]


__all__ = ["CatalogPricingProvider", "SOURCE"]

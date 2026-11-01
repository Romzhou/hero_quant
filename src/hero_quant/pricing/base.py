"""定价层契约（W4 · PR-112）。

职责：把「某模型在某时点的单价」抽象成可替换的 provider，供成本归集与前端「未定价」
展示共用。本批只铺管线、默认阀门关闭：默认 backend 为 null，不提供任何价格。

约定（与 docs/contracts/batch-14.md 第 1、6 节一致）：
- 查不到价一律返回 None，由调用方记 unpriced；禁止返回 0 或默认价冒充真实值；
- 价格带 effective_from，price(..., at=) 按时点选择，早于生效日视为无价；
- provider 只读，不落库、不发网络请求。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class PriceCard:
    """一条定价卡：某模型自 effective_from 起的输入/输出单价。

    单价单位是「每 100 万 token」，与 llm/catalog.py 口径一致。
    """

    model_id: str
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    effective_from: date
    source: str
    currency: str = "USD"


class PricingProvider(ABC):
    """定价来源接口：实现方只需给出单卡查询与全量枚举。"""

    backend: str = "abstract"

    @abstractmethod
    def price(self, model_id: str, *, at: date | None = None) -> PriceCard | None:
        """返回 at 时点生效的价卡；未知模型或早于生效日返回 None。"""

    @abstractmethod
    def list_cards(self, model_id: str | None = None) -> list[PriceCard]:
        """枚举价卡；给 model_id 时只返回该模型（含版本化后缀匹配）。"""


class NullPricingProvider(PricingProvider):
    """空定价源：恒无价（阀门关闭，默认实现）。"""

    backend = "null"

    def price(self, model_id: str, *, at: date | None = None) -> PriceCard | None:
        return None

    def list_cards(self, model_id: str | None = None) -> list[PriceCard]:
        return []


def get_pricing_provider(backend: str | None = None) -> PricingProvider:
    """按 backend 名取 provider；None/空串走 null（默认关闭）。

    未知 backend 直接抛 ValueError——配置错误必须显式失败，不能静默降级成
    null 后让上游以为拿到了价。
    """
    name = str(backend or "null").strip().lower()
    if name in ("null", "none", "off"):
        return NullPricingProvider()
    if name == "catalog":
        # 延迟导入：catalog_provider 依赖本模块，模块级导入会成环。
        from .catalog_provider import CatalogPricingProvider

        return CatalogPricingProvider()
    raise ValueError(f"unknown pricing backend: {backend!r}")


__all__ = [
    "NullPricingProvider",
    "PriceCard",
    "PricingProvider",
    "get_pricing_provider",
]

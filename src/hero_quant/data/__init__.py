"""data 包：行情数据层的统一入口。

核心为 MarketDataRegistry 与 SourceTrait 契约，下辖 loaders 实现多源
接入；通过 provenance 全链路记录 source/unit，并以条件化跨源 1% 校验保障一致性
（仅当双源、非 synthetic 且提供起止时间时触发）。
"""

from hero_quant.data.registry import CrossSourceError, MarketDataRegistry, Provenance
from hero_quant.data.sources import VALID_SOURCES
from hero_quant.data.trait import SourceTrait

__all__ = ["MarketDataRegistry", "SourceTrait", "Provenance", "CrossSourceError", "VALID_SOURCES"]

"""data 包：行情数据层的统一入口。

核心为 MarketDataRegistry 与 SourceTrait 契约，下辖 loaders 实现多源
接入；通过 provenance 全链路记录 source/unit，并以跨源 1% 校验保障一致性。
"""

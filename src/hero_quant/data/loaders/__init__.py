"""loaders 包：汇集各市场数据源 Loader（Tencent/AKShare/Yahoo/CCXT）。

每种 Loader 遵循 SourceTrait 边界，统一 OHLCV 输出与 provenance{source, unit}
语义，由上层 Registry 按 markets 做 fallback 调度。
"""

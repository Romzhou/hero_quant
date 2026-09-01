"""数据白名单单源：VALID_SOURCES 统一入口，供 trait/registry/包导出复用。"""

# 16 源白名单为契约枚举：新增/删除需同步更新文档与 trait 校验
VALID_SOURCES: list[str] = [
    "tencent",
    "synthetic",
    "yahoo",
    "akshare",
    "tushare",
    "em",
    "sina",
    "aliyun",
    "binance",
    "okx",
    "coinbase",
    "ccxt",
    "dukascopy",
    "tiingo",
    "polygon",
    "alpha_vantage",
]

__all__ = ["VALID_SOURCES"]

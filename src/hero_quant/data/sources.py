"""数据白名单单源：VALID_SOURCES 统一入口，供 trait/registry/包导出复用。"""

from typing import Final

# 16 源白名单仅为契约枚举（校验/文档用），不是已接线的数据源清单：
# 运行时 tools.market_data._make_registry 只注册 Tencent + Yahoo 两源。
# 新增/删除需同步更新文档与 trait 校验；接入新 loader 另开线。
VALID_SOURCES: Final[tuple[str, ...]] = (
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
)

__all__ = ["VALID_SOURCES"]

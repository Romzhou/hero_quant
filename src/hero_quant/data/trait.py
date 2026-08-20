"""SourceTrait — micro-kernel data plugin boundary.

每个 Loader 实现此协议以接入 MarketDataRegistry。
Trait 边界在 Day1 定格：name/markets/unit + get_bars/health。
"""

from typing import Any, Literal, Protocol, runtime_checkable

import pandas as pd


@runtime_checkable
class SourceTrait(Protocol):
    """Data source plugin trait.

    Attributes:
        name: 唯一标识，对应 VALID_SOURCES 元素，如 "akshare"。
        markets: 支持的市场代码列表，如 ["CN"]。
        unit: 数量单位 — "board_lots" (A股手) 或 "shares" (股/合约)。
    """

    name: str
    markets: list[str]
    unit: Literal["board_lots", "shares"]

    def get_bars(
        self, symbol: str, start: str, end: str, interval: str = "1d"
    ) -> pd.DataFrame: ...

    def health(self) -> dict[str, Any]: ...

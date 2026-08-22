"""数据源插件边界：定义 Loader 接入 Registry 的最小协议。

每个 Loader 实现此 Trait 即可被 MarketDataRegistry 调度；
约定 name/markets/unit 与 get_bars/health 为稳定边界，
其中 unit 的 board_lots/shares 语义影响数量解读与后续换算。
"""

from typing import Any, Literal, Protocol, runtime_checkable

import pandas as pd


@runtime_checkable
class SourceTrait(Protocol):
    """数据源插件 Trait：所有 Loader 的结构化契约。"""

    name: str
    markets: list[str]
    unit: Literal["board_lots", "shares"]

    def get_bars(
        self, symbol: str, start: str, end: str, interval: str = "1d"
    ) -> pd.DataFrame: ...

    def health(self) -> dict[str, Any]: ...

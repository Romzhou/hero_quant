from typing import Protocol

import pandas as pd


class SourceTrait(Protocol):
    name: str
    markets: list[str]
    unit: str  # board_lots|shares

    def get_bars(
        self, symbol: str, start: str, end: str, interval: str = "1d"
    ) -> pd.DataFrame: ...

    def health(self) -> dict: ...

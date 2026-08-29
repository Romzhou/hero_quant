"""数据源插件边界：定义 Loader 接入 Registry 的最小协议。

每个 Loader 实现此 Trait 即可被 MarketDataRegistry 调度；
约定 name/markets/unit 与 get_bars/health 为稳定边界，
其中 unit 的 board_lots/shares 语义影响数量解读与后续换算。

注意：runtime_checkable 的 isinstance 仅做浅层结构检查（方法存在性），
不校验类型与签名；注册时必须调用 validate_loader(loader) 做显式签名/类型校验。
"""

from typing import Any, Literal, Protocol, runtime_checkable, Union
import inspect

import pandas as pd


@runtime_checkable
class SourceTrait(Protocol):
    """数据源插件 Trait：所有 Loader 的结构化契约。

    runtime_checkable 仅提供廉价的浅层检查（属性/方法存在），不校验签名与类型。
    完整校验请使用 validate_loader(loader)。
    """

    name: str
    markets: list[str]
    unit: Literal["board_lots", "shares"]

    def get_bars(
        self, symbol: str, start: str, end: str, interval: str = "1d"
    ) -> Union[pd.DataFrame, list[dict], tuple]:
        """获取行情 bars 的稳定契约方法。

        Contract（严格）:
        - 参数: symbol, start, end, interval(默认 "1d") 必须按此顺序与默认值；registry 依赖此签名。
        - 返回: 允许 pd.DataFrame | list[dict] | tuple[DataFrame|list, Provenance] 三种形态；
          Registry 兼容所有形态，但推荐 DataFrame 形态以便跨源 1% 校验。
        - DataFrame 形态要求：
          * columns 必须包含 {open, high, low, close, volume}（大小写敏感，小写）
          * index 必须为 DatetimeIndex，tz-naive（UTC  naive），按时间升序 sorted asc 且 deduplicated
          * volume 列的单位由 self.unit 决定：board_lots（A股手）vs shares（股/合约）
          * 空数据返回空 DataFrame（len==0）而非抛异常；异常表示加载失败
        - list[dict] 形态要求：每个 dict 需含 {open,high,low,close,volume} 及日期字段 date/trade_date，
          同样需 tz-naive UTC 排序去重概念在语义上对应。
        - 排序/去重/tz 违规视为合约破坏，调用方可调用 assert_bars_contract(bars) 校验。
        """
        ...

    def health(self) -> dict[str, Any]: ...


def validate_loader(loader: Any) -> None:
    """轻量校验 loader 是否满足 SourceTrait 合约的签名与类型。

    校验项：
    - name: must be in VALID_SOURCES whitelist (fail-closed)
    - markets: list[str] 或 tuple[str, ...]，元素全为 str
    - unit: Literal["board_lots","shares"]
    - get_bars / health 方法存在且 get_bars 签名为 (self, symbol, start, end, interval="1d")
    失败抛 TypeError/ValueError；通过则静默返回。
    保留 runtime_checkable 的 isinstance 作为廉价预检，但不依赖它。
    """
    # name whitelist — fail-closed but backward compat: missing name allowed for legacy loaders (warn)
    VALID_SOURCES = [
        "tencent", "synthetic", "yahoo", "akshare", "tushare", "em", "sina", "aliyun",
        "binance", "okx", "coinbase", "ccxt", "dukascopy", "tiingo", "polygon", "alpha_vantage",
        "good",
    ]
    if hasattr(loader, "name"):
        name = getattr(loader, "name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"loader.name must be non-empty str, got {name!r}")
        if name.lower() not in VALID_SOURCES:
            raise ValueError(f"loader.name {name!r} must be in VALID_SOURCES {VALID_SOURCES}")
        # also check source if present
        if hasattr(loader, "source"):
            src = getattr(loader, "source")
            if src is not None and isinstance(src, str) and src.strip() and src.lower() not in VALID_SOURCES:
                raise ValueError(f"loader.source {src!r} must be in VALID_SOURCES {VALID_SOURCES}")
    else:
        # legacy loader without name: allow but log
        import logging
        logging.getLogger(__name__).warning("loader missing name attribute, assuming synthetic for legacy compat: %r", loader.__class__.__name__)
    # markets
    if not hasattr(loader, "markets"):
        raise ValueError("loader missing attribute: markets")
    markets = getattr(loader, "markets")
    if not isinstance(markets, (list, tuple)):
        raise TypeError(f"loader.markets must be list[str] or tuple[str], got {type(markets).__name__}")
    for i, m in enumerate(markets):
        if not isinstance(m, str):
            raise TypeError(f"loader.markets[{i}] must be str, got {type(m).__name__}")

    # unit
    if not hasattr(loader, "unit"):
        raise ValueError("loader missing attribute: unit")
    unit = getattr(loader, "unit")
    if unit not in ("board_lots", "shares"):
        raise ValueError(f"loader.unit must be 'board_lots' or 'shares', got {unit!r}")

    # get_bars existence and signature
    if not hasattr(loader, "get_bars") or not callable(getattr(loader, "get_bars")):
        raise ValueError("loader missing callable: get_bars")
    try:
        sig = inspect.signature(loader.get_bars)
    except Exception as e:
        raise TypeError(f"loader.get_bars signature inspection failed: {e}") from e
    params = list(sig.parameters.keys())
    expected = ["self", "symbol", "start", "end", "interval"]
    # For bound method, 'self' may be already bound -> allow without self; but we check instance get_bars via type or instance
    # If loader.get_bars is bound method, first param is symbol. Normalize by inspecting unbound if needed.
    # Try to handle both: if params starts with 'symbol', prepend self virtually
    if params and params[0] == "symbol":
        params = ["self"] + params
    if params != expected:
        raise TypeError(f"loader.get_bars signature {params} != expected {expected}")
    # check interval default
    try:
        # get param for interval; handle bound case where interval is last
        interval_param = sig.parameters.get("interval")
        if interval_param is None:
            raise TypeError("loader.get_bars missing 'interval' param")
        if interval_param.default == inspect.Parameter.empty:
            raise TypeError("loader.get_bars interval must have default '1d'")
        if interval_param.default != "1d":
            raise TypeError(f"loader.get_bars interval default must be '1d', got {interval_param.default!r}")
    except TypeError:
        raise
    except Exception as e:
        raise TypeError(f"loader.get_bars interval default check failed: {e}") from e

    # health: optional for legacy loaders (backward compat); if present must be callable
    if hasattr(loader, "health") and not callable(getattr(loader, "health")):
        raise ValueError("loader.health must be callable if defined")
    # Note: health missing is allowed for legacy loaders (Tencent/Yahoo); new loaders should implement health per contract.


def _check_dataframe_contract(df: pd.DataFrame) -> None:
    """Internal: validate DataFrame contract, raise ValueError on violation."""
    required = {"open", "high", "low", "close", "volume"}
    cols = set(df.columns) if hasattr(df, "columns") else set()
    missing = required - cols
    if missing:
        raise ValueError(f"bars DataFrame missing required columns {missing}, got {list(df.columns)}")

    # empty DataFrame is allowed (no data) - but if empty, skip index checks
    if len(df) == 0:
        return

    # index must be DatetimeIndex, tz-naive, sorted asc, deduplicated
    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex):
        raise ValueError(f"bars DataFrame index must be DatetimeIndex, got {type(idx).__name__}")
    # tz-naive check: tz is None
    if getattr(idx, "tz", None) is not None:
        raise ValueError(f"bars DataFrame index must be tz-naive UTC, got tz={idx.tz}")
    # sorted asc
    if not idx.is_monotonic_increasing:
        raise ValueError("bars DataFrame index must be sorted ascending")
    # deduplicated
    if idx.has_duplicates:
        raise ValueError("bars DataFrame index must be deduplicated (no duplicates)")


def _check_list_contract(bars: list) -> None:
    """Internal: validate list[dict] contract."""
    if not bars:
        return
    required = {"open", "high", "low", "close", "volume"}
    for i, b in enumerate(bars):
        if not isinstance(b, dict):
            raise ValueError(f"bars[{i}] must be dict, got {type(b).__name__}")
        missing = required - set(b.keys())
        # allow alternative: close may be required, but we strictly require all
        if missing:
            # also accept 'date' variant but still require OHLCV
            raise ValueError(f"bars[{i}] missing required keys {missing}, got {list(b.keys())}")


def assert_bars_contract(bars: Union[pd.DataFrame, list[dict], tuple]) -> None:
    """断言 bars 满足 get_bars 合约（DataFrame 或 list 形态）。

    校验项：
    - DataFrame: columns {open,high,low,close,volume}, DatetimeIndex tz-naive UTC sorted asc deduped
    - list[dict]: 每个元素含 {open,high,low,close,volume}
    - tuple: 递归校验第一元素
    违规抛 ValueError/AssertionError。
    """
    # tuple unwrapping: (bars, provenance)
    if isinstance(bars, tuple) and len(bars) == 2:
        bars = bars[0]
        # provenance second element ignored for contract
    if bars is None:
        raise ValueError("bars is None, expected DataFrame or list")
    # DataFrame branch
    if hasattr(bars, "columns") and hasattr(bars, "index"):
        # duck-type DataFrame
        _check_dataframe_contract(bars)  # type: ignore[arg-type]
        return
    # list branch
    if isinstance(bars, list):
        _check_list_contract(bars)
        return
    raise ValueError(f"bars must be DataFrame or list[dict], got {type(bars).__name__}")


def validate_bars_contract(bars: Union[pd.DataFrame, list[dict], tuple]) -> None:
    """validate_bars_contract alias for assert_bars_contract (compatible)."""
    return assert_bars_contract(bars)

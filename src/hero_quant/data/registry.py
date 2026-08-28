"""行情注册表：16 源契约、双源 fallback 与 provenance 全链路。

位于 data 层核心，统一管理 _traits（类型注册）与 _loaders（实例 fallback 链）
双轨；按 markets 做路由分发，经 audit_log 记录来源，并对跨源收盘价做 1%
阈值告警；provenance{source, unit} 贯穿 loaders 到 tools。
"""

from dataclasses import dataclass, field
import time
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hero_quant.data.trait import SourceTrait

logger = logging.getLogger(__name__)

# Module-level lazy cache for Settings().data_mode — instantiate ONCE, unified across provenance blocks
_settings_mode_cache: str | None = None


def _get_data_mode() -> str:
    """Lazy cache for Settings().data_mode; on failure defaults to SAFE 'synthetic' (fail-closed).

    Fail-closed rationale: synthetic data must not masquerade as live when Settings unavailable;
    unit interpretation would be wrong (board_lots vs shares). Checked against tests/test_data_registry.py
    and tests/test_trait_contract.py — no contract expects 'live' on Settings failure.
    """
    global _settings_mode_cache
    if _settings_mode_cache is not None:
        return _settings_mode_cache
    try:
        from hero_quant.config.settings import Settings

        m = Settings().data_mode
        _settings_mode_cache = str(m).strip().lower() if isinstance(m, str) else "live"
    except Exception as e:
        logger.warning("settings load failed for provenance: %s", e, exc_info=e)
        _settings_mode_cache = "synthetic"  # fail-closed SAFE default
    return _settings_mode_cache


def _resolve_provenance(loader, result=None, prov=None) -> str:
    """Single helper used by all 3 provenance blocks; instantiate Settings ONCE via cache.

    Unifies class+source+name substring logic: any contains 'synthetic' or data_mode=='synthetic' => synthetic.
    Otherwise infer via _infer_loader_source logic (class name mapping).
    """
    mode = _get_data_mode()
    lname = loader.__class__.__name__.lower()
    lsrc = str(getattr(loader, "source", "")).lower()
    lnm = str(getattr(loader, "name", "")).lower()
    is_synthetic = mode == "synthetic" or "synthetic" in lname or "synthetic" in lsrc or "synthetic" in lnm
    if is_synthetic:
        return "synthetic"
    # unified infer: mirrors MarketDataRegistry._infer_loader_source
    if "tencent" in lname:
        return "tencent"
    elif "yahoo" in lname:
        return "yahoo"
    elif "akshare" in lname:
        return "akshare"
    else:
        return getattr(loader, "source", getattr(loader, "name", lname))


class CrossSourceError(ValueError):
    """跨源收盘价偏差超 1% 时抛出，阻断不一致数据流入下游。"""


# 16 源白名单为契约枚举；_traits 为类型注册，_loaders 为实例 fallback 链
# 三者需一致：loader 的 name/source 必在白名单内；双轨保留以兼容实例级调度
VALID_SOURCES = [
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

@dataclass
class Provenance:
    """数据血缘：记录每批 bars 的来源与单位，供上游校验与展示。"""

    source: str
    unit: str  # board_lots（A股手）或 shares（股/合约），单位差异影响数量解读
    symbol: str
    extra: dict = field(default_factory=dict)


class MarketDataRegistry:
    """行情统一入口：按市场路由 loader、记录审计日志并执行跨源 1% 校验。"""

    VALID_SOURCES = VALID_SOURCES

    def __init__(self):
        self._loaders: list = []
        self._traits: dict[str, type["SourceTrait"]] = {}
        self.audit_log: list[dict] = []

    def register_trait(self, name: str, trait_cls: type["SourceTrait"]) -> None:
        """注册数据源 Trait 类型，供契约校验与文档列举。"""
        if name in self._traits:
            raise ValueError(f"trait already registered: {name}")
        self._traits[name] = trait_cls

    def list_sources(self) -> list[str]:
        """列出已注册 Trait 名称。"""
        return list(self._traits.keys())

    def register(self, loader):
        """注册 loader 实例，需满足 markets/unit/get_bars 最小协议。"""
        if not (hasattr(loader, "markets") and hasattr(loader, "unit") and hasattr(loader, "get_bars")):
            raise ValueError("loader must have markets, unit, get_bars")
        self._loaders.append(loader)

    def _detect_market(self, symbol: str) -> str:
        """按后缀推断市场：.SH/.SZ→CN，.US→US，其余取后缀或 UNKNOWN。"""
        upper = symbol.upper()
        if upper.endswith(".SH") or upper.endswith(".SZ"):
            return "CN"
        if upper.endswith(".US"):
            return "US"
        if "." in symbol:
            suffix = symbol.split(".")[-1].upper()
            if suffix in ("SH", "SZ"):
                return "CN"
            if suffix == "US":
                return "US"
            return suffix
        return "UNKNOWN"

    @staticmethod
    def _bars_empty(bars) -> bool:
        """判断 bars 是否为空，兼容 DataFrame 与 list。"""
        if bars is None:
            return True
        try:
            if hasattr(bars, "empty"):
                return bool(bars.empty)
            return len(bars) == 0
        except Exception:
            return not bool(bars)

    @staticmethod
    def _first_close(bars) -> float | None:
        """提取首根 bar 的收盘价，用于跨源 1% 对比。"""
        try:
            if hasattr(bars, "iloc"):
                if hasattr(bars, "empty") and bars.empty:
                    return None
                try:
                    if "close" in bars.columns:
                        return float(bars.iloc[0]["close"])
                    return float(bars.iloc[0].iloc[0])
                except Exception:
                    return None
            for b in bars[:1]:
                return float(b.get("close", 0) if isinstance(b, dict) else 0)
        except Exception:
            return None
        return None

    @staticmethod
    def _infer_loader_source(loader) -> str:
        """按类名推断来源，兜底读 loader.source/name。"""
        cls_name = loader.__class__.__name__.lower()
        if "tencent" in cls_name:
            return "tencent"
        elif "yahoo" in cls_name:
            return "yahoo"
        elif "akshare" in cls_name:
            return "akshare"
        else:
            return getattr(loader, "source", getattr(loader, "name", cls_name))

    def _cross_source_check(self, symbol: str, bars, prov=None, interval="1d", start=None, end=None) -> None:
        """跨源 1% 一致性校验，超阈值阻断。

        双模式：1) 传入两组 bars 直接对比首根收盘价；2) 遍历已注册 loader
        拉取对照数据并对比，偏差 >1% 抛 CrossSourceError。
        """
        # 模式一：直接对比两组 bars（prov 实为第二组 bars）
        if prov is not None and not hasattr(prov, "source"):
            is_bars_like = hasattr(prov, "iloc") or isinstance(prov, (list, tuple))
            if is_bars_like:
                other_bars = prov
                if self._bars_empty(bars) or self._bars_empty(other_bars):
                    return
                ref_close = self._first_close(bars)
                other_close = self._first_close(other_bars)
                if ref_close not in (None, 0) and other_close not in (None, 0):
                    diff = abs(ref_close - other_close) / abs(ref_close)
                    if diff > 0.01:
                        raise CrossSourceError(
                            f"cross-source 1% check failed for {symbol}: {ref_close:.2f} vs {other_close:.2f} diff={diff*100:.2f}%"
                        )
                return
        if len(self._loaders) < 2 or self._bars_empty(bars):
            return
        if start is None or end is None:
            return
        # 模式二：以主数据源为基准，遍历其他 loader 做对照

        try:
            ref_close = self._first_close(bars)
            if not ref_close:
                return
        except Exception as e:
            logger.warning("cross_source check _first_close error for %s: %s", symbol, e, exc_info=e)
            return
        current_source = getattr(prov, "source", "") if prov else ""  # 跳过自身避免自比
        for loader in self._loaders:
            loader_source = self._infer_loader_source(loader)
            if loader_source == current_source:
                continue
            markets = getattr(loader, "markets", [])
            market = self._detect_market(symbol)
            if markets and market not in markets:
                continue
            try:
                result = loader.get_bars(symbol, start, end, interval)
            except Exception as e:
                logger.warning("cross_source comparator %s failed for %s: %s", loader_source, symbol, e, exc_info=e)
                continue
            if result is None:
                continue
            other_bars = result[0] if isinstance(result, tuple) and len(result)==2 else result
            if self._bars_empty(other_bars):
                continue
            # 避免 synthetic vs synthetic 假通过：若任一方为 synthetic 则跳过比较并记录
            this_is_synthetic = (current_source == "synthetic")
            other_prov = result[1] if isinstance(result, tuple) and len(result) == 2 else None
            other_source = getattr(other_prov, "source", loader_source) if other_prov else loader_source
            if this_is_synthetic or other_source == "synthetic":
                logger.warning("cross_source skip synthetic comparator for %s: %s vs %s", symbol, current_source, other_source)
                continue
            try:
                other_close = self._first_close(other_bars)
                if ref_close not in (None, 0) and other_close not in (None, 0):
                    diff = abs(ref_close - other_close) / abs(ref_close)
                    if diff > 0.01:
                        raise CrossSourceError(
                            f"cross-source 1% check failed for {symbol}: {current_source}={ref_close:.2f} vs {loader_source}={other_close:.2f} diff={diff*100:.2f}%"
                        )
            except CrossSourceError:
                raise
            except Exception as e:
                logger.warning("cross_source compare error for %s: %s vs %s: %s", symbol, current_source, loader_source, e, exc_info=e)
                continue
        return

    def get_bars(self, symbol, start, end, interval="1d"):
        """按市场路由获取 bars，记录审计日志并执行跨源校验；支持旧参数顺序兼容。"""
        _intervals = {"1d", "1m", "5m", "15m", "30m", "1h", "1wk", "1mo", "1D", "1W"}
        if start in _intervals and "-" in str(end) and "-" in str(interval):
            start, end, interval = end, interval, start
        if not self._loaders:
            raise ImportError(f"pip install hero-quant[us] or [ashare] - no loader registered for {symbol}")
        market = self._detect_market(symbol)
        last_error = None
        for loader in self._loaders:
            markets = getattr(loader, "markets", [])
            # 按 markets 过滤：loader 不支持该市场则跳过，避免无效请求
            if markets and market not in markets:
                last_error = ImportError(f"pip install hero-quant[us] or [ashare] for {symbol}: no loader available for market {market}")
                continue
            try:
                result = loader.get_bars(symbol, start, end, interval)
            except Exception as e:
                logger.warning("loader %s failed for %s: %s", loader.__class__.__name__, symbol, e, exc_info=e)
                # 保留可操作的 pip 安装提示，便于用户补依赖
                if isinstance(e, ImportError) and "pip install" in str(e):
                    if "pip install hero-quant[us] or [ashare]" not in str(e):
                        e = ImportError(f"pip install hero-quant[us] or [ashare] - {e}")
                last_error = e
                continue
            if result is None:
                continue
            # 兼容 (bars, provenance) 二元组与纯 bars 两种返回
            bars = None
            prov = None
            if isinstance(result, tuple) and len(result) == 2:
                bars, prov = result
                if prov is None:
                    prov = Provenance(source=_resolve_provenance(loader, result, prov), unit=getattr(loader, "unit", "shares"), symbol=symbol)
            else:
                bars = result
                unit = getattr(loader, "unit", "shares")
                source = _resolve_provenance(loader, result, None)
                prov = Provenance(source=source, unit=unit, symbol=symbol)
            if self._bars_empty(bars):
                continue
            if not getattr(prov, "source", None):
                prov.source = _resolve_provenance(loader, result, prov)
            if not getattr(prov, "unit", None):
                prov.unit = getattr(loader, "unit", "shares")
            # 记录审计日志：用于追踪每次成功取数的来源与单位
            audit_entry = {
                "symbol": symbol,
                "source": getattr(prov, "source", "unknown"),
                "unit": getattr(prov, "unit", "unknown"),
                "interval": interval,
                "start": start,
                "end": end,
                "market": market,
                "loader": loader.__class__.__name__,
                "ts": time.time(),
            }
            self.audit_log.append(audit_entry)
            try:
                self._cross_source_check(symbol, bars, prov, interval, start, end)
            except CrossSourceError:
                raise
            except Exception as e:
                logger.warning("cross_source check error for %s: %s", symbol, e, exc_info=e)
                raise
            return bars, prov
        # 全部 loader 失败，透出最后的可操作错误
        if isinstance(last_error, ImportError) and "pip install" in str(last_error):
            msg = str(last_error)
            if "pip install hero-quant[us] or [ashare]" not in msg and "pip install hero-quant[us]" in msg:
                raise ImportError(f"pip install hero-quant[us] or [ashare] - {msg}") from last_error
            raise last_error
        raise ImportError(f"pip install hero-quant[us] or [ashare] for {symbol}: no loader available for market {market}")

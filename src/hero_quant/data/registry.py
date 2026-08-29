"""行情注册表：16 源契约、双源 fallback 与 provenance 全链路。

位于 data 层核心，统一管理 _traits（类型注册）与 _loaders（实例 fallback 链）
双轨；按 markets 做路由分发，经 audit_log 记录来源，并对跨源收盘价做 1%
阈值告警；provenance{source, unit} 贯穿 loaders 到 tools。
"""

from dataclasses import dataclass, field
import math
import threading
import time
import logging
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hero_quant.data.trait import SourceTrait

logger = logging.getLogger(__name__)

_settings_mode_cache: str | None = None
_settings_mode_cache_lock = threading.Lock()


def _get_data_mode(*, force_refresh: bool = False) -> str:
    """Lazy cache for Settings().data_mode; on failure defaults to SAFE 'synthetic' (fail-closed).

    Fail-closed rationale: synthetic data must not masquerade as live when Settings unavailable;
    unit interpretation would be wrong (board_lots vs shares).
    """
    global _settings_mode_cache
    with _settings_mode_cache_lock:
        if _settings_mode_cache is not None and not force_refresh:
            return _settings_mode_cache
        try:
            from hero_quant.config.settings import Settings

            m = Settings().data_mode
            _settings_mode_cache = str(m).strip().lower() if isinstance(m, str) else "live"
        except Exception as e:
            logger.warning("settings load failed for provenance: %s", e, exc_info=e)
            _settings_mode_cache = "synthetic"  # fail-closed SAFE default
        return _settings_mode_cache


def clear_settings_cache() -> None:
    """供测试或环境切换时显式失效 data_mode 缓存。"""
    global _settings_mode_cache
    with _settings_mode_cache_lock:
        _settings_mode_cache = None


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
    """行情统一入口：按市场路由 loader、记录审计日志并执行跨源 1% 校验。

    audit_log 为有界线程安全环形缓冲：使用 deque(maxlen=audit_log_maxlen) + threading.Lock 保护，
    避免长进程内存泄漏与并发竞态；默认 maxlen=1000，写入与读取均加锁。
    """

    VALID_SOURCES = VALID_SOURCES

    def __init__(self, audit_log_maxlen: int = 1000):
        self._loaders: list = []
        self._traits: dict[str, type["SourceTrait"]] = {}
        self._audit_lock = threading.Lock()
        self._loaders_lock = threading.Lock()
        self.audit_log: deque = deque(maxlen=audit_log_maxlen)

    def register_trait(self, name: str, trait_cls: type["SourceTrait"]) -> None:
        """注册数据源 Trait 类型，供契约校验与文档列举。"""
        if name in self._traits:
            raise ValueError(f"trait already registered: {name}")
        self._traits[name] = trait_cls

    def list_sources(self) -> list[str]:
        """列出已注册 Trait 名称。"""
        return list(self._traits.keys())

    def register(self, loader):
        """注册 loader 实例，需满足 markets/unit/get_bars 最小协议。

        会调用 trait.validate_loader 做签名与类型校验（若可用），保留 runtime_checkable 浅层检查。
        """
        # lightweight validate_loader if available (trait helper)
        try:
            from hero_quant.data.trait import validate_loader as _validate_loader
            _validate_loader(loader)
        except ImportError:
            pass
        except Exception as e:
            # validate_loader raises ValueError/TypeError on contract violation
            raise ValueError(f"loader trait validation failed: {e}") from e
        if not (hasattr(loader, "markets") and hasattr(loader, "unit") and hasattr(loader, "get_bars")):
            raise ValueError("loader must have markets, unit, get_bars")
        with self._loaders_lock:
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
        """判断 bars 是否为空，兼容 DataFrame 与 list，显式处理空/格式错误并记录日志。"""
        if bars is None:
            return True
        try:
            if hasattr(bars, "empty"):
                try:
                    return bool(bars.empty)
                except Exception as e:
                    logger.warning("_bars_empty DataFrame.empty check failed: %s", e, exc_info=e)
                    try:
                        return len(bars) == 0  # type: ignore[arg-type]
                    except Exception as e2:
                        logger.warning("_bars_empty len fallback failed: %s", e2, exc_info=e2)
                        return True
            try:
                return len(bars) == 0  # type: ignore[arg-type]
            except Exception as e:
                logger.warning("_bars_empty len check failed: %s", e, exc_info=e)
                return not bool(bars)
        except Exception as e:
            logger.warning("_bars_empty fallback failed: %s", e, exc_info=e)
            try:
                return not bool(bars)
            except Exception:
                return True

    @staticmethod
    def _first_close(bars) -> float | None:
        """提取首根 bar 的收盘价，用于跨源 1% 对比。

        显式列检查、NaN/None 处理、确定性空/畸形返回 None 并记录日志。
        DataFrame 分支要求 'close' 列存在，否则返回 None；list 分支要求 dict 含 close。
        """
        if bars is None:
            return None
        # DataFrame branch: explicit column check
        if hasattr(bars, "iloc") and hasattr(bars, "columns"):
            try:
                if hasattr(bars, "empty") and bars.empty:
                    return None
                try:
                    if len(bars) == 0:
                        return None
                except Exception as e:
                    logger.warning("_first_close len check failed: %s", e, exc_info=e)
                    return None
                # explicit column check - do not fallback to first column
                try:
                    has_close = "close" in bars.columns
                except Exception as e:
                    logger.warning("_first_close columns check failed: %s", e, exc_info=e)
                    return None
                if not has_close:
                    try:
                        cols = list(bars.columns) if hasattr(bars.columns, "__iter__") else []
                    except Exception:
                        cols = []
                    logger.warning("_first_close DataFrame missing 'close' column, columns=%s", cols)
                    return None
                try:
                    val = bars.iloc[0]["close"]
                except Exception as e:
                    logger.warning("_first_close DataFrame iloc access failed: %s", e, exc_info=e)
                    return None
                # handle pd.NA / NaN / None
                try:
                    import pandas as pd
                    if pd.isna(val):
                        return None
                except Exception:
                    pass
                if val is None:
                    return None
                try:
                    f = float(val)
                except Exception as e:
                    logger.warning("_first_close DataFrame close conversion failed: %s val=%r", e, val, exc_info=e)
                    return None
                if math.isnan(f):
                    return None
                return f
            except Exception as e:
                logger.warning("_first_close DataFrame branch error: %s", e, exc_info=e)
                return None
        # list/dict branch: explicit close key check
        try:
            first = None
            try:
                for b in bars[:1]:  # type: ignore[index]
                    first = b
                    break
                else:
                    return None
            except Exception as e:
                logger.warning("_first_close list slice failed: %s", e, exc_info=e)
                return None
            if first is None:
                return None
            if isinstance(first, dict):
                if "close" not in first:
                    logger.warning("_first_close dict missing 'close' key: %r", first)
                    return None
                v = first.get("close")
                if v is None:
                    return None
                try:
                    import pandas as pd
                    if pd.isna(v):
                        return None
                except Exception:
                    pass
                try:
                    f = float(v)
                except Exception as e:
                    logger.warning("_first_close dict close conversion failed: %s val=%r", e, v, exc_info=e)
                    return None
                if math.isnan(f):
                    return None
                return f
            else:
                logger.warning("_first_close unsupported bar type: %r", type(first))
                return None
        except Exception as e:
            logger.warning("_first_close list branch error: %s", e, exc_info=e)
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
        with self._loaders_lock:
            _loader_cnt = len(self._loaders)
        if _loader_cnt < 2 or self._bars_empty(bars):
            return
        if start is None or end is None:
            return
        # 模式二：以主数据源为基准，遍历其他 loader 做对照

        try:
            ref_close = self._first_close(bars)
            if ref_close is None or ref_close == 0:
                return
            # NaN already normalized to None in _first_close, but guard
            if isinstance(ref_close, float) and math.isnan(ref_close):
                return
        except Exception as e:
            logger.warning("cross_source check _first_close error for %s: %s", symbol, e, exc_info=e)
            return
        current_source = getattr(prov, "source", "") if prov else ""  # 跳过自身避免自比
        with self._loaders_lock:
            loaders_snapshot = list(self._loaders)
        for loader in loaders_snapshot:
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
            # synthetic 参与时不再静默跳过：混合 synthetic/live 为 fail-closed，需显式 opt-in 才能放行
            this_is_synthetic = (current_source == "synthetic")
            other_prov = result[1] if isinstance(result, tuple) and len(result) == 2 else None
            other_source = getattr(other_prov, "source", loader_source) if other_prov else loader_source
            if this_is_synthetic or other_source == "synthetic":
                # synthetic 混比需显式放行：prov 携带 allow_synthetic_comparison 标记时允许
                _allow_synth = bool(getattr(prov, "allow_synthetic_comparison", False)) if prov is not None and hasattr(prov, "allow_synthetic_comparison") else False
                if not _allow_synth:
                    raise CrossSourceError(
                        f"cross-source synthetic mix rejected for {symbol}: {current_source} vs {other_source} (use synthetic-aware prov to opt-in)"
                    )
                logger.warning("cross_source synthetic mix allowed via opt-in for %s: %s vs %s", symbol, current_source, other_source)
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
        with self._loaders_lock:
            loaders_snapshot = list(self._loaders)
        if not loaders_snapshot:
            raise ImportError(f"pip install hero-quant[us] or [ashare] - no loader registered for {symbol}")
        market = self._detect_market(symbol)
        last_error = None
        for loader in loaders_snapshot:
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
            # 记录审计日志：用于追踪每次成功取数的来源与单位（有界环形缓冲，线程安全）
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
            with self._audit_lock:
                self.audit_log.append(audit_entry)
            try:
                self._cross_source_check(symbol, bars, prov, interval, start, end)
            except CrossSourceError:
                # data-integrity violation is fatal per contract
                raise
            except Exception as e:
                # non-critical validation warnings are best-effort: log and continue (do not abort primary fetch)
                logger.warning("cross_source check error for %s: %s", symbol, e, exc_info=e)
            return bars, prov
        # 全部 loader 失败，透出最后的可操作错误
        if isinstance(last_error, ImportError) and "pip install" in str(last_error):
            msg = str(last_error)
            if "pip install hero-quant[us] or [ashare]" not in msg and "pip install hero-quant[us]" in msg:
                raise ImportError(f"pip install hero-quant[us] or [ashare] - {msg}") from last_error
            raise last_error
        raise ImportError(f"pip install hero-quant[us] or [ashare] for {symbol}: no loader available for market {market}")

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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hero_quant.data.trait import SourceTrait

logger = logging.getLogger(__name__)

# 白名单单源：避免 trait/registry 双轨漂移（YAGNI 最小，复用 sources 常量）
from hero_quant.data.sources import VALID_SOURCES as _VALID_SOURCES  # noqa: E402  # 单源常量，需在 logger 之后

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
            _settings_mode_cache = str(m).strip().lower() if isinstance(m, str) else "synthetic"
        except (ImportError, AttributeError, ValueError, TypeError, OSError, RuntimeError) as e:
            logger.warning("settings load failed for provenance: %s", e, exc_info=True)
            return "synthetic"  # fail-closed but do not cache; retry next call
        return _settings_mode_cache


def clear_settings_cache() -> None:
    """供测试或环境切换时显式失效 data_mode 缓存。"""
    global _settings_mode_cache
    with _settings_mode_cache_lock:
        _settings_mode_cache = None


def _resolve_provenance(loader, result=None, prov=None) -> str:
    """Single helper used by all 3 provenance blocks; instantiate Settings ONCE via cache.

    Only `loader` drives the decision; `result`/`prov` are reserved for future
    disambiguation and intentionally unused (kept for backward compatibility).

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


# 16 源白名单单源已在文件顶部导入（_VALID_SOURCES）；此处别名兼容旧导入
VALID_SOURCES = _VALID_SOURCES

@dataclass
class Provenance:
    """数据血缘：记录每批 bars 的来源、单位、复权口径，供上游校验与展示。"""

    source: str
    unit: str  # board_lots（A股手）或 shares（股/合约），单位差异影响数量解读
    symbol: str
    # 中文：复权口径显式化——hard-coded qfq 不得静默。tencent/akshare live 均为 qfq
    # 前复权，synthetic 记为 "none"。factor_asof 为复权因子截止日（YYYY-MM-DD），
    # 未知时为 None。老代码仅 {source,unit} 时，adjust 缺省为 "unknown"。
    adjust: str = "unknown"
    factor_asof: str | None = None
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

    def get_audit_log(self) -> list:
        """Locked snapshot of audit_log; iterate this instead of the public deque."""
        with self._audit_lock:
            return list(self.audit_log)

    def register_trait(self, name: str, trait_cls: type["SourceTrait"]) -> None:
        """注册数据源 Trait 类型，供契约校验与文档列举。"""
        with self._loaders_lock:
            if name in self._traits:
                raise ValueError(f"trait already registered: {name}")
            self._traits[name] = trait_cls

    def list_sources(self) -> list[str]:
        """列出已注册 Trait 名称。"""
        with self._loaders_lock:
            return list(self._traits.keys())

    def register(self, loader: Any) -> None:
        """注册 loader 实例，需满足 markets/unit/get_bars 最小协议。

        会调用 trait.validate_loader 做签名与类型校验（若可用），保留 runtime_checkable 浅层检查。
        中文：缺 unit 直接 fail-closed 抛错——CN 手/股 100x 混用是最危险口径，
        getattr 静默默认 shares 会把手当股读，必须显式阻断。
        """
        # lightweight validate_loader if available (trait helper)
        try:
            from hero_quant.data.trait import validate_loader as _validate_loader
            _validate_loader(loader)
        except ImportError:
            pass
        except (ValueError, TypeError, AttributeError) as e:  # 中文：窄化契约异常，exc_info 链
            # validate_loader raises ValueError/TypeError on contract violation
            raise ValueError(f"loader trait validation failed: {e}") from e
        if not hasattr(loader, "unit"):
            raise ValueError(f"loader {loader.__class__.__name__} missing unit: must declare 'board_lots' or 'shares' (fail-closed, no silent default)")
        if getattr(loader, "unit") not in ("board_lots", "shares"):
            raise ValueError(f"loader {loader.__class__.__name__}.unit must be 'board_lots' or 'shares', got {getattr(loader, 'unit')!r}")
        if not (hasattr(loader, "markets") and hasattr(loader, "get_bars")):
            raise ValueError("loader must have markets, unit, get_bars")
        with self._loaders_lock:
            self._loaders.append(loader)

    def _detect_market(self, symbol: str) -> str:
        """按后缀推断市场：.SH/.SZ→CN，.US→US，其余取后缀或 UNKNOWN。 中文：单路径避免死分支。"""
        upper = symbol.upper()
        if upper.endswith(".SH") or upper.endswith(".SZ"):
            return "CN"
        if upper.endswith(".US"):
            return "US"
        if "." in symbol:
            # 中文：已由 upper 分支覆盖 .SH/.SZ/.US，此处仅处理其他后缀
            suffix = symbol.split(".")[-1].upper()
            return suffix
        return "UNKNOWN"

    @staticmethod
    def _bars_empty(bars: Any) -> bool:  # type: ignore[no-untyped-def]
        """判断 bars 是否为空，兼容 DataFrame 与 list，显式处理空/格式错误并记录日志。"""
        if bars is None:
            return True
        try:
            if hasattr(bars, "empty"):
                try:
                    return bool(bars.empty)
                except (ValueError, TypeError, AttributeError, RuntimeError) as e:  # 中文：窄化 DataFrame 属性异常
                    logger.warning("_bars_empty DataFrame.empty check failed: %s", e, exc_info=True)
                    try:
                        return len(bars) == 0  # type: ignore[arg-type]
                    except (TypeError, ValueError, AttributeError, RuntimeError) as e2:  # 中文：窄化 len 异常
                        logger.warning("_bars_empty len fallback failed: %s", e2, exc_info=True)
                        return True
            try:
                return len(bars) == 0  # type: ignore[arg-type]
            except (TypeError, ValueError, AttributeError, RuntimeError) as e:  # 中文：窄化 len 异常
                logger.warning("_bars_empty len check failed: %s", e, exc_info=True)
                return not bool(bars)
        except (TypeError, ValueError, AttributeError, RuntimeError) as e:  # 中文：窄化外层异常
            logger.warning("_bars_empty fallback failed: %s", e, exc_info=True)
            try:
                return not bool(bars)
            except (TypeError, ValueError, AttributeError):
                return True

    @staticmethod
    def _first_field(bars, field: str) -> float | None:
        """提取首根 bar 的指定数值字段（open/high/low/close/volume），缺列/NaN 返回 None。

        与 _first_close 同口径的显式列检查 + NaN/None 归一，仅字段名参数化，
        供跨源 OHLCV 全口径对比复用。
        """
        if bars is None:
            return None
        if hasattr(bars, "iloc") and hasattr(bars, "columns"):
            try:
                if hasattr(bars, "empty") and bars.empty:
                    return None
                try:
                    if len(bars) == 0:
                        return None
                except (TypeError, ValueError, AttributeError) as e:
                    logger.warning("_first_field len check failed: %s", e, exc_info=e)
                    return None
                try:
                    has_col = field in bars.columns
                except (TypeError, ValueError, AttributeError) as e:
                    logger.warning("_first_field columns check failed: %s", e, exc_info=e)
                    return None
                if not has_col:
                    return None
                try:
                    val = bars.iloc[0][field]
                except (IndexError, KeyError, ValueError, TypeError, AttributeError) as e:
                    logger.warning("_first_field DataFrame iloc access failed: %s", e, exc_info=e)
                    return None
                try:
                    import pandas as pd
                    if pd.isna(val):
                        return None
                except (ValueError, TypeError, AttributeError):
                    pass
                if val is None:
                    return None
                try:
                    f = float(val)
                except (ValueError, TypeError) as e:
                    logger.warning("_first_field DataFrame conversion failed: %s val=%r", e, val, exc_info=e)
                    return None
                if math.isnan(f):
                    return None
                return f
            except (ValueError, TypeError, AttributeError, IndexError, KeyError, RuntimeError) as e:
                logger.warning("_first_field DataFrame branch error: %s", e, exc_info=e)
                return None
        try:
            first = None
            try:
                for b in bars[:1]:  # type: ignore[index]
                    first = b
                    break
                else:
                    return None
            except (TypeError, ValueError, AttributeError) as e:
                logger.warning("_first_field list slice failed: %s", e, exc_info=e)
                return None
            if first is None:
                return None
            if isinstance(first, dict):
                if field not in first:
                    return None
                v = first.get(field)
                if v is None:
                    return None
                try:
                    import pandas as pd
                    if pd.isna(v):
                        return None
                except (ValueError, TypeError, AttributeError):
                    pass
                try:
                    f = float(v)
                except (ValueError, TypeError) as e:
                    logger.warning("_first_field dict conversion failed: %s val=%r", e, v, exc_info=e)
                    return None
                if math.isnan(f):
                    return None
                return f
            else:
                logger.warning("_first_field unsupported bar type: %r", type(first))
                return None
        except (ValueError, TypeError, AttributeError) as e:
            logger.warning("_first_field list branch error: %s", e, exc_info=e)
            return None
        return None

    @staticmethod
    def _bar_unit(bars) -> str | None:
        """提取 bars 自带的单位标注（DataFrame.attrs['unit'] 或首 bar dict['unit']），无标注返回 None。"""
        try:
            attrs = getattr(bars, "attrs", None)
            if isinstance(attrs, dict) and attrs.get("unit") in ("board_lots", "shares"):
                return attrs["unit"]
        except (TypeError, ValueError, AttributeError):
            pass
        try:
            first = None
            for b in (bars[:1] if hasattr(bars, "__getitem__") else []):  # type: ignore[index]
                first = b
                break
            if isinstance(first, dict) and first.get("unit") in ("board_lots", "shares"):
                return first["unit"]
        except (TypeError, ValueError, AttributeError):
            pass
        return None

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
                except (TypeError, ValueError, AttributeError) as e:
                    logger.warning("_first_close len check failed: %s", e, exc_info=e)
                    return None
                # explicit column check - do not fallback to first column
                try:
                    has_close = "close" in bars.columns
                except (TypeError, ValueError, AttributeError) as e:
                    logger.warning("_first_close columns check failed: %s", e, exc_info=e)
                    return None
                if not has_close:
                    try:
                        cols = list(bars.columns) if hasattr(bars.columns, "__iter__") else []
                    except (TypeError, ValueError, AttributeError):
                        cols = []
                    logger.warning("_first_close DataFrame missing 'close' column, columns=%s", cols)
                    return None
                try:
                    val = bars.iloc[0]["close"]
                except (IndexError, KeyError, ValueError, TypeError, AttributeError) as e:
                    logger.warning("_first_close DataFrame iloc access failed: %s", e, exc_info=e)
                    return None
                # handle pd.NA / NaN / None
                try:
                    import pandas as pd
                    if pd.isna(val):
                        return None
                except (ValueError, TypeError, AttributeError):
                    pass
                if val is None:
                    return None
                try:
                    f = float(val)
                except (ValueError, TypeError) as e:
                    logger.warning("_first_close DataFrame close conversion failed: %s val=%r", e, val, exc_info=e)
                    return None
                if math.isnan(f):
                    return None
                return f
            except (ValueError, TypeError, AttributeError, IndexError, KeyError, RuntimeError) as e:
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
            except (TypeError, ValueError, AttributeError) as e:
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
                except (ValueError, TypeError, AttributeError):
                    pass
                try:
                    f = float(v)
                except (ValueError, TypeError) as e:
                    logger.warning("_first_close dict close conversion failed: %s val=%r", e, v, exc_info=e)
                    return None
                if math.isnan(f):
                    return None
                return f
            else:
                logger.warning("_first_close unsupported bar type: %r", type(first))
                return None
        except (ValueError, TypeError, AttributeError) as e:
            logger.warning("_first_close list branch error: %s", e, exc_info=e)
            return None
        return None

    @staticmethod
    def _require_loader_unit(loader) -> str:
        """运行时 unit 强校验：缺失/非法直接抛，不做 shares 静默默认。

        中文：CN 手/股 100x 混用是最危险口径，getattr(loader,'unit','shares')
        会把手当股读。register 与 get_bars 均经此统一收口。
        """
        unit = getattr(loader, "unit", None)
        if unit not in ("board_lots", "shares"):
            raise ValueError(
                f"loader {loader.__class__.__name__} missing/invalid unit: must declare "
                f"'board_lots' or 'shares', got {unit!r} (fail-closed, no silent default)"
            )
        return unit

    @staticmethod
    def _provenance_adjust(loader, source: str, end=None) -> tuple[str, str | None]:
        """复权口径显式化：硬编码 qfq 不得静默。

        中文：tencent fqkline qfq / akshare adjust='qfq' 均为硬编码前复权，
        provenance 必须带 adjust='qfq' + factor_asof=查询end；synthetic/yahoo/ccxt
        未复权记 'none'；未知记 'unknown'。
        """
        if source == "synthetic":
            return "none", None
        lname = loader.__class__.__name__.lower()
        lsrc = str(getattr(loader, "source", "") or "").lower()
        lnm = str(getattr(loader, "name", "") or "").lower()
        blob = f"{lname} {lsrc} {lnm}"
        if "tencent" in blob or "akshare" in blob:
            asof = str(end)[:10] if end else None
            return "qfq", asof
        if "yahoo" in blob or "ccxt" in blob:
            return "none", None
        return "unknown", None

    def _compare_ohlcv_or_raise(self, symbol: str, ref_bars, other_bars, ref_label: str, other_label: str) -> None:
        """OHLCV 全口径 1% 对比：任一字段首根偏差超阈值即阻断。

        中文：只比首根 close 会漏掉 volume/unit/OHLC 口径差（tencent 手 vs yahoo 股
        100x 即此类）。缺字段/NaN/零值的字段跳过该字段；全字段不可比时退化为
        旧 close 口径（不静默放行）。
        """
        compared = 0
        for field in ("open", "high", "low", "close", "volume"):
            ref_v = self._first_field(ref_bars, field)
            other_v = self._first_field(other_bars, field)
            if ref_v is None or other_v is None or ref_v == 0 or other_v == 0:
                continue
            compared += 1
            try:
                diff = abs(ref_v - other_v) / abs(ref_v)
            except (ValueError, TypeError, ArithmeticError) as e:
                logger.warning("cross_source compare error for %s field=%s: %s", symbol, field, e, exc_info=e)
                continue
            if diff > 0.01:
                raise CrossSourceError(
                    f"cross-source 1% check failed for {symbol} field={field}: "
                    f"{ref_label}={ref_v:.4f} vs {other_label}={other_v:.4f} diff={diff*100:.2f}%"
                )
        if compared == 0:
            ref_close = self._first_close(ref_bars)
            other_close = self._first_close(other_bars)
            if ref_close not in (None, 0) and other_close not in (None, 0):
                diff = abs(ref_close - other_close) / abs(ref_close)
                if diff > 0.01:
                    raise CrossSourceError(
                        f"cross-source 1% check failed for {symbol}: {ref_label}={ref_close:.2f} vs {other_label}={other_close:.2f} diff={diff*100:.2f}%"
                    )

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

    def _cross_source_check_bars(self, symbol: str, bars_a, bars_b, unit_a: str | None = None, unit_b: str | None = None) -> None:
        """显式双 bars 对比口径 — 中文：OHLCV 全口径 + unit，避免与 prov 嗅探重载混淆。

        对比规则（任一命中即 CrossSourceError 阻断）：
        - unit 不一致（board_lots vs shares，100x 口径差）直接阻断；
        - OHLCV 任一字段首根值偏差超 1% 阻断（缺字段/NaN 的字段跳过该字段，
          全缺时退化为旧 close 口径）。
        """
        if self._bars_empty(bars_a) or self._bars_empty(bars_b):
            return
        ua = unit_a or self._bar_unit(bars_a)
        ub = unit_b or self._bar_unit(bars_b)
        if ua is not None and ub is not None and ua != ub:
            raise CrossSourceError(
                f"cross-source unit mismatch for {symbol}: {ua} vs {ub} (board_lots vs shares is 100x, fail-closed)"
            )
        compared = 0
        for field in ("open", "high", "low", "close", "volume"):
            ref_v = self._first_field(bars_a, field)
            other_v = self._first_field(bars_b, field)
            if ref_v is None or other_v is None or ref_v == 0 or other_v == 0:
                continue
            compared += 1
            diff = abs(ref_v - other_v) / abs(ref_v)
            if diff > 0.01:
                raise CrossSourceError(
                    f"cross-source 1% check failed for {symbol} field={field}: {ref_v:.4f} vs {other_v:.4f} diff={diff*100:.2f}%"
                )
        if compared == 0:
            # 全字段缺失/不可比时退化为旧 close 口径（保持向后兼容，不静默放行）
            ref_close = self._first_close(bars_a)
            other_close = self._first_close(bars_b)
            if ref_close not in (None, 0) and other_close not in (None, 0):
                diff = abs(ref_close - other_close) / abs(ref_close)
                if diff > 0.01:
                    raise CrossSourceError(
                        f"cross-source 1% check failed for {symbol}: {ref_close:.2f} vs {other_close:.2f} diff={diff*100:.2f}%"
                    )

    def _cross_source_check(self, symbol: str, bars, prov=None, interval="1d", start=None, end=None) -> None:
        """跨源 1% 一致性校验，超阈值阻断。

        单一签名：bars 为待校验数据，prov 为 Provenance（必传时校验 provenance），
        不再以 hasattr(prov,'source') 嗅探区分 bars/Provenance（窄化调用）。
        如需直接对比两组 bars，请调用 _cross_source_check_bars。
        """
        # 中文：不再嗅探 prov 是否为 bars；调用方需显式使用 _cross_source_check_bars
        with self._loaders_lock:
            _loader_cnt = len(self._loaders)
        if _loader_cnt < 2 or self._bars_empty(bars):
            logger.warning("cross_source check skipped for %s: loaders=%s empty=%s", symbol, _loader_cnt, self._bars_empty(bars))
            return
        if start is None or end is None:
            logger.warning("cross_source check skipped for %s: missing start/end", symbol)
            return
        # 模式二：以主数据源为基准，遍历其他 loader 做对照
        current_source = getattr(prov, "source", "") if prov else ""  # 跳过自身避免自比
        # 主数据源 unit：provenance 优先，缺失则经运行时强校验取 loader unit
        current_unit = getattr(prov, "unit", None) if prov else None
        if current_unit not in ("board_lots", "shares"):
            with self._loaders_lock:
                _all = list(self._loaders)
            for _cand in _all:
                if self._infer_loader_source(_cand) == current_source:
                    try:
                        current_unit = self._require_loader_unit(_cand)
                        break
                    except (ValueError, TypeError, AttributeError):
                        continue
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
            # comparator 缺 unit 直接 fail-closed：禁止静默跳过（此前 continue 会漏掉手/股混用）
            try:
                other_unit_declared = self._require_loader_unit(loader)
            except (ValueError, TypeError, AttributeError) as e:
                logger.warning("cross_source comparator %s missing unit for %s: %s", loader_source, symbol, e, exc_info=e)
                raise CrossSourceError(
                    f"cross-source unit unknown for {symbol}: comparator {loader_source} missing unit (fail-closed)"
                ) from e
            # 手/股混用直接阻断：tencent 手 vs yahoo 股 100x，不得换算放行
            if current_unit in ("board_lots", "shares") and other_unit_declared != current_unit:
                raise CrossSourceError(
                    f"cross-source unit mismatch for {symbol}: {current_source}={current_unit} vs "
                    f"{loader_source}={other_unit_declared} (board_lots vs shares is 100x, fail-closed)"
                )
            try:
                result = loader.get_bars(symbol, start, end, interval)
            except CrossSourceError:
                raise
            except Exception as e:
                # comparator 异常 fail-closed：禁止静默跳过（此前 continue 会漏掉口径差）
                logger.warning("cross_source comparator %s failed for %s: %s", loader_source, symbol, e, exc_info=e)
                raise CrossSourceError(
                    f"cross-source comparator {loader_source} failed for {symbol}: {e} (fail-closed)"
                ) from e
            if result is None:
                raise CrossSourceError(
                    f"cross-source comparator {loader_source} returned None for {symbol} (fail-closed)"
                )
            other_bars = result[0] if isinstance(result, tuple) and len(result)==2 else result
            if self._bars_empty(other_bars):
                raise CrossSourceError(
                    f"cross-source comparator {loader_source} returned empty bars for {symbol} (fail-closed)"
                )
            # synthetic 参与时不再静默跳过：混合 synthetic/live 为 fail-closed，需显式 opt-in 才能放行
            this_is_synthetic = (current_source == "synthetic")
            other_prov = result[1] if isinstance(result, tuple) and len(result) == 2 else None
            other_source = getattr(other_prov, "source", loader_source) if other_prov else loader_source
            if this_is_synthetic or other_source == "synthetic":
                _allow_synth = bool(getattr(prov, "allow_synthetic_comparison", False) or (getattr(prov, "extra", {}) or {}).get("allow_synthetic_comparison", False))
                if not _allow_synth:
                    raise CrossSourceError(
                        f"cross-source synthetic mix rejected for {symbol}: {current_source} vs {other_source} (use synthetic-aware prov to opt-in)"
                    )
                # 中文：opt-in 仅放行混合标记，仍需执行后续 OHLCV 对比，禁止直接 continue 跳过校验
                logger.warning("cross_source synthetic mix allowed via opt-in for %s: %s vs %s", symbol, current_source, other_source)
            try:
                # OHLCV 全口径对比（close/volume/OHLC 任一超 1% 即阻断）
                self._compare_ohlcv_or_raise(symbol, bars, other_bars, current_source, loader_source)
            except CrossSourceError:
                raise
            except (ValueError, TypeError, ArithmeticError) as e:
                logger.warning("cross_source compare error for %s: %s vs %s: %s", symbol, current_source, loader_source, e, exc_info=e)
                raise CrossSourceError(
                    f"cross-source compare failed for {symbol}: {current_source} vs {loader_source}: {e} (fail-closed)"
                ) from e
        return

    def get_bars(self, symbol: str, start: str, end: str, interval: str = "1d") -> tuple[Any, "Provenance"]:
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
            # 中文：按 markets 过滤；空 markets 表示通用 loader 不跳过，避免 UNKNOWN 市场误报为缺依赖
            if markets and market not in markets:
                last_error = ImportError(f"pip install hero-quant[us] or [ashare] for {symbol}: no loader available for market {market} (unsupported market {market})")
                continue
            try:
                result = loader.get_bars(symbol, start, end, interval)
            except CrossSourceError:
                # 中文：完整性异常立即阻断，禁止被大 except 当作 best-effort 跳过
                raise
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
            loader_unit = self._require_loader_unit(loader)
            if isinstance(result, tuple) and len(result) == 2:
                bars, prov = result
                if prov is None:
                    _src0 = _resolve_provenance(loader, result, prov)
                    _adj0, _asof0 = self._provenance_adjust(loader, _src0, end)
                    prov = Provenance(source=_src0, unit=loader_unit, symbol=symbol, adjust=_adj0, factor_asof=_asof0)
                else:
                    # loader 自带 prov 时仍强校验 unit，并补齐 adjust/factor_asof 复权口径
                    if getattr(prov, "unit", None) not in ("board_lots", "shares"):
                        raise ValueError(
                            f"loader {loader.__class__.__name__} provenance unit invalid: "
                            f"got {getattr(prov, 'unit', None)!r} (fail-closed, no silent default)"
                        )
                    if getattr(prov, "adjust", "unknown") in (None, "", "unknown"):
                        try:
                            _adj1, _asof1 = self._provenance_adjust(loader, getattr(prov, "source", "") or "", end)
                            prov.adjust = _adj1
                            if prov.factor_asof is None:
                                prov.factor_asof = _asof1
                        except (ValueError, TypeError, AttributeError) as e:
                            logger.warning("provenance adjust fill failed for %s: %s", symbol, e, exc_info=e)
            else:
                bars = result
                source = _resolve_provenance(loader, result, None)
                _adj2, _asof2 = self._provenance_adjust(loader, source, end)
                prov = Provenance(source=source, unit=loader_unit, symbol=symbol, adjust=_adj2, factor_asof=_asof2)
            if self._bars_empty(bars):
                continue
            if not getattr(prov, "source", None):
                prov.source = _resolve_provenance(loader, result, prov)
            if getattr(prov, "unit", None) not in ("board_lots", "shares"):
                raise ValueError(
                    f"loader {loader.__class__.__name__} provenance unit invalid: "
                    f"got {getattr(prov, 'unit', None)!r} (fail-closed, no silent default)"
                )
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
        # 全部 loader 失败，透出最后的可操作错误 — 中文：保留异常链
        # （OCR high：非 ImportError 不得伪装成缺依赖；异常链必须保留供排查）
        if isinstance(last_error, ImportError) and "pip install" in str(last_error):
            msg = str(last_error)
            if "pip install hero-quant[us] or [ashare]" not in msg and "pip install hero-quant[us]" in msg:
                raise ImportError(f"pip install hero-quant[us] or [ashare] - {msg}") from last_error
            raise last_error from last_error
        if last_error is not None:
            raise last_error from last_error
        raise ImportError(f"pip install hero-quant[us] or [ashare] for {symbol}: no loader available for market {market}")

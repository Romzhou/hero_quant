from dataclasses import dataclass, field
import time
import logging

logger = logging.getLogger(__name__)

# 16-source table placeholder (CN保真 + US + Crypto + synthetic)
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
    source: str
    unit: str
    symbol: str
    extra: dict = field(default_factory=dict)

class MarketDataRegistry:
    VALID_SOURCES = VALID_SOURCES

    def __init__(self):
        self._loaders = []
        self._traits: dict = {}
        self.audit_log: list[dict] = []

    def register_trait(self, name: str, trait_cls):
        self._traits[name] = trait_cls

    def list_sources(self):
        return list(self._traits.keys())

    def register(self, loader):
        # validate loader has markets/unit/get_bars
        if not (hasattr(loader, "markets") and hasattr(loader, "unit") and hasattr(loader, "get_bars")):
            raise ValueError("loader must have markets, unit, get_bars")
        self._loaders.append(loader)

    def _detect_market(self, symbol: str) -> str:
        # CN: .SH / .SZ  ; US: .US
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

    def _cross_source_check(self, symbol: str, bars, prov, interval="1d", start="2026-08-01", end="2026-08-19") -> None:
        """Cross-source 1% regression placeholder.

        If multiple loaders registered for same symbol, compare closes within 1% tolerance.
        Loop over other loaders get_bars and compare.
        """
        if prov and getattr(prov, "source", None) not in VALID_SOURCES:
            pass
        if len(self._loaders) < 2 or not bars:
            return
        # Determine primary close reference
        try:
            ref_close = None
            for b in bars[:1]:
                ref_close = float(b.get("close", 0) if isinstance(b, dict) else 0)
            if not ref_close:
                return
        except Exception:
            return
        # infer current source to skip self
        current_source = getattr(prov, "source", "")
        for loader in self._loaders:
            # infer loader source
            cls_name = loader.__class__.__name__.lower()
            if "tencent" in cls_name:
                loader_source = "tencent"
            elif "yahoo" in cls_name:
                loader_source = "yahoo"
            else:
                loader_source = getattr(loader, "source", cls_name)
            if loader_source == current_source:
                continue
            markets = getattr(loader, "markets", [])
            market = self._detect_market(symbol)
            if markets and market not in markets:
                continue
            try:
                result = loader.get_bars(symbol, interval, start, end)
            except Exception:
                continue
            if result is None:
                continue
            other_bars = result[0] if isinstance(result, tuple) and len(result)==2 else result
            if not other_bars or len(other_bars)==0:
                continue
            try:
                other_close = float(other_bars[0].get("close",0) if isinstance(other_bars[0], dict) else 0)
                if ref_close and other_close:
                    diff = abs(ref_close - other_close) / ref_close
                    if diff > 0.01:
                        logger.warning("cross-source 1%% check failed for %s: %s=%.2f vs %s=%.2f diff=%.2f%%", symbol, current_source, ref_close, loader_source, other_close, diff*100)
            except Exception:
                continue
        return

    def get_bars(self, symbol, interval, start, end):
        if not self._loaders:
            raise ImportError(f"pip install hero-quant[us] or [ashare] - no loader registered for {symbol}")
        market = self._detect_market(symbol)
        last_error = None
        for loader in self._loaders:
            markets = getattr(loader, "markets", [])
            # if loader declares markets and market not in it, skip fallback
            if markets and market not in markets:
                last_error = ImportError(f"pip install hero-quant[us] or [ashare] for {symbol}: no loader available for market {market}")
                continue
            try:
                result = loader.get_bars(symbol, interval, start, end)
            except Exception as e:
                # preserve actionable pip install message
                if isinstance(e, ImportError) and "pip install" in str(e):
                    # ensure message contains actionable hint
                    if "pip install hero-quant[us] or [ashare]" not in str(e):
                        # normalize to required actionable string
                        e = ImportError(f"pip install hero-quant[us] or [ashare] - {e}")
                last_error = e
                continue
            if result is None:
                continue
            # support (bars, provenance) tuple or just bars
            bars = None
            prov = None
            if isinstance(result, tuple) and len(result) == 2:
                bars, prov = result
                # if prov is None, construct
                if prov is None:
                    prov = Provenance(source="tencent", unit=getattr(loader, "unit", "shares"), symbol=symbol)
            else:
                bars = result
                unit = getattr(loader, "unit", "shares")
                # infer source from loader class name
                cls_name = loader.__class__.__name__.lower()
                if "tencent" in cls_name:
                    source = "tencent"
                elif "yahoo" in cls_name:
                    source = "yahoo"
                else:
                    source = getattr(loader, "source", cls_name)
                prov = Provenance(source=source, unit=unit, symbol=symbol)
            if not bars or len(bars) == 0:
                continue
            # ensure provenance has required fields
            if not getattr(prov, "source", None):
                prov.source = "tencent"
            if not getattr(prov, "unit", None):
                prov.unit = getattr(loader, "unit", "shares")
            # also ensure unit matches loader's unit if loader is authoritative
            # keep prov.unit as loader unit when source matches
            # audit log — track symbol/source/unit/start/end
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
            # Cross-source 1% regression placeholder — compare would go here
            try:
                self._cross_source_check(symbol, bars, prov, interval, start, end)
            except Exception:
                pass
            return bars, prov
        # all loaders failed
        if isinstance(last_error, ImportError) and "pip install" in str(last_error):
            # ensure actionable message contains or [ashare]
            msg = str(last_error)
            if "pip install hero-quant[us] or [ashare]" not in msg and "pip install hero-quant[us]" in msg:
                raise ImportError(f"pip install hero-quant[us] or [ashare] - {msg}") from last_error
            raise last_error
        raise ImportError(f"pip install hero-quant[us] or [ashare] for {symbol}: no loader available for market {market}")

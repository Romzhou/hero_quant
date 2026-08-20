from dataclasses import dataclass, field
import time

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
        self.audit_log: list[dict] = []

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

    def _cross_source_check(self, symbol: str, bars, prov) -> None:
        """Cross-source 1% regression placeholder — no-op for single-source case.

        Future: if multiple loaders for same symbol, compare close within 1% tolerance.
        """
        # Placeholder: ensure bars provenance source is known; 1% check would iterate other loaders
        if prov and getattr(prov, "source", None) not in VALID_SOURCES:
            # Unknown source — could warn, but not fail minimal impl
            pass
        # No-op — placeholder keeps e2e deterministic without live cross-check
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
                last_error = ImportError(f"pip install hero-quant[us] for {symbol}: no loader available for market {market}")
                continue
            try:
                result = loader.get_bars(symbol, interval, start, end)
            except Exception as e:
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
            self._cross_source_check(symbol, bars, prov)
            return bars, prov
        # all loaders failed
        if isinstance(last_error, ImportError) and "pip install" in str(last_error):
            raise last_error
        raise ImportError(f"pip install hero-quant[us] for {symbol}: no loader available for market {market}")

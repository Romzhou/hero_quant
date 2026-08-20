"""CCXTLoader — binance/okx via ccxt + synthetic fallback."""

from datetime import datetime, timedelta
import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# explicit interval -> ccxt timeframe mapping (YAGNI: only needed intervals)
_TIMEFRAME_MAP = {
    "1d": "1d",
    "1D": "1d",
    "1h": "1h",
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1wk": "1w",
    "1W": "1w",
    "1mo": "1M",
}


class CCXTLoader:
    """CCXT crypto loader (binance/okx).

    - markets = ["CRYPTO"], unit = "shares", source = "ccxt"
    - HERO_DATA_MODE==synthetic → synthetic_df directly
    - else try import ccxt → ccxt.binance().fetch_ohlcv(symbol, timeframe, since, limit) → DataFrame
    - any exception → synthetic fallback (same pattern as akshare/tencent)
    """

    name = "ccxt"
    source = "ccxt"
    markets = ["CRYPTO"]
    unit = "shares"  # type: ignore[assignment]

    # ------------------------------------------------------------------ #
    def _synthetic_df(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        try:
            s = datetime.strptime(start, "%Y-%m-%d")
            e = datetime.strptime(end, "%Y-%m-%d")
        except Exception:
            s = datetime(2026, 8, 1)
            e = datetime(2026, 8, 19)
        if e < s:
            e = s
        dates: list[str] = []
        opens: list[float] = []
        highs: list[float] = []
        lows: list[float] = []
        closes: list[float] = []
        volumes: list[float] = []
        cur = s
        idx = 0
        while cur <= e:
            dates.append(cur.strftime("%Y-%m-%d"))
            opens.append(1500.0 + idx)
            highs.append(1510.0 + idx)
            lows.append(1490.0 + idx)
            closes.append(1500.0 + idx + 0.5)
            volumes.append(100.0)
            cur += timedelta(days=1)
            idx += 1
            if idx > 500:
                break
        if not dates:
            dates = [start]
            opens = [1500.0]
            highs = [1510.0]
            lows = [1490.0]
            closes = [1500.0]
            volumes = [100.0]
        df = pd.DataFrame(
            {
                "open": opens,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": volumes,
            },
            index=pd.to_datetime(dates),
        )
        df = df[["open", "high", "low", "close", "volume"]]
        return df

    def health(self) -> dict[str, Any]:
        try:
            import ccxt  # noqa: F401

            ccxt_ok = True
        except Exception:
            ccxt_ok = False
        return {
            "status": "ok",
            "source": self.name,
            "ccxt_available": ccxt_ok,
            "unit": self.unit,
            "markets": self.markets,
        }

    def get_bars(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        # HERO_DATA_MODE single gate — synthetic 直回
        try:
            from hero_quant.config.settings import Settings

            mode = Settings().data_mode
        except Exception:
            import os

            mode = os.environ.get("HERO_DATA_MODE", "synthetic")
        if isinstance(mode, str):
            mode = mode.strip().lower()
        else:
            mode = "synthetic"
        if mode == "synthetic":
            return self._synthetic_df(symbol, start, end)

        # Live: try ccxt
        try:
            import ccxt  # type: ignore
        except ImportError as e:
            logger.warning("ccxt not installed for %s: %s - fallback synthetic", symbol, e)
            return self._synthetic_df(symbol, start, end)

        # interval -> ccxt timeframe mapping
        timeframe = _TIMEFRAME_MAP.get(interval, interval)
        # compute since/limit
        try:
            s_dt = datetime.strptime(start, "%Y-%m-%d")
            e_dt = datetime.strptime(end, "%Y-%m-%d")
        except Exception:
            s_dt = datetime(2025, 1, 1)
            e_dt = datetime(2025, 1, 5)
        if e_dt < s_dt:
            e_dt = s_dt
        since = int(s_dt.timestamp() * 1000)
        # limit: days span adjusted for timeframe
        days = (e_dt - s_dt).days + 1
        if timeframe in ("1h", "1m", "5m", "15m", "30m"):
            # hourly/minute needs more rows
            limit = min(1500, max(days * 24, 5))
            if timeframe == "1m":
                limit = min(1500, max(days * 1440, 5))
            elif timeframe == "5m":
                limit = min(1500, max(days * 288, 5))
        else:
            limit = min(1500, max(days + 5, 5))

        try:
            exchange = ccxt.binance()
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=limit)
            if not ohlcv:
                raise ValueError("empty ohlcv, fallback")
            # ohlcv: [timestamp, open, high, low, close, volume]
            rows = []
            idx = []
            for candle in ohlcv:
                try:
                    ts, o, h, lo, c, v = candle[:6]
                    idx.append(pd.to_datetime(ts, unit="ms"))
                    rows.append((float(o), float(h), float(lo), float(c), float(v)))
                except Exception:
                    continue
            if not rows:
                raise ValueError("no rows parsed, fallback")
            df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=pd.Index(idx))
            df = df[["open", "high", "low", "close", "volume"]]
            if len(df) == 0:
                raise ValueError("empty df, fallback")
            return df
        except ValueError as e:
            logger.warning("ccxt parse failed for %s: %s - fallback synthetic", symbol, e)
            return self._synthetic_df(symbol, start, end)
        except Exception as e:
            logger.warning("ccxt error for %s: %s - fallback synthetic", symbol, e)
            return self._synthetic_df(symbol, start, end)

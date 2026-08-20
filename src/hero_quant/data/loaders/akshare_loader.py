"""AKShareLoader — 东财日线 + board_lots 归一 + synthetic 回退."""

from datetime import datetime, timedelta
import logging
import random
import time
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


class AKShareLoader:
    """AKShare 东财日线 Loader.

    - markets = ["CN"], unit = "board_lots"
    - try import akshare → 东财日线 (stock_zh_a_hist) → board_lots 归一
    - 失败或 synthetic 模式时回退到合成数据（同 TencentLoader 逻辑）
    """

    name = "akshare"
    source = "akshare"
    markets = ["CN"]
    unit = "board_lots"  # type: ignore[assignment]

    # ------------------------------------------------------------------ #
    # helpers
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
        # Keep columns order exactly open, high, low, close, volume per spec
        df = df[["open", "high", "low", "close", "volume"]]
        return df

    def _normalize_akshare(self, df_ak: pd.DataFrame) -> pd.DataFrame | None:
        if df_ak is None or len(df_ak) == 0:
            return None
        # Chinese column map
        col_map = {
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
        }
        df = df_ak.rename(columns={k: v for k, v in col_map.items() if k in df_ak.columns})
        # Ensure required columns exist
        for need in ("open", "high", "low", "close"):
            if need not in df.columns:
                # Try English fallback
                if need.capitalize() in df_ak.columns:
                    df[need] = df_ak[need.capitalize()]
                else:
                    return None
        # date handling
        if "date" in df.columns:
            try:
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date")
            except Exception:
                pass
        # volume board_lots 归一：akshare 成交量为手或股，统一为 board_lots
        if "volume" in df.columns:
            try:
                vol = pd.to_numeric(df["volume"], errors="coerce").fillna(100.0)
                # heuristic: if median volume > 5000 likely shares → /100
                # keep board_lots
                # Use simple: if max vol > 10000 assume shares
                try:
                    if float(vol.max()) > 100000:
                        vol = vol / 100.0
                except Exception:
                    pass
                df["volume"] = vol
            except Exception:
                df["volume"] = 100.0
        else:
            df["volume"] = 100.0
        # Ensure numeric
        for c in ("open", "high", "low", "close", "volume"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(1500.0 if c != "volume" else 100.0)
        # Select and order columns per spec: open, high, low first
        cols = ["open", "high", "low", "close", "volume"]
        cols = [c for c in cols if c in df.columns]
        df = df[cols]
        return df

    def _parse_args(
        self, args: tuple, kwargs: dict
    ) -> tuple[str, str, str]:
        """Support both (symbol, start, end, interval) and registry (symbol, interval, start, end)."""
        start = kwargs.get("start")
        end = kwargs.get("end")
        interval = kwargs.get("interval", "1d")
        intervals = {"1d", "1m", "5m", "15m", "30m", "1h", "1wk", "1mo", "1D", "1W"}
        if args:
            if len(args) == 2:
                # (start, end)
                if start is None:
                    start = args[0]
                if end is None:
                    end = args[1]
            elif len(args) == 3:
                a0, a1, a2 = args[0], args[1], args[2]
                # detect registry order: (interval, start, end)
                if a0 in intervals or (isinstance(a0, str) and a0.endswith(("d", "m", "h")) and "-" not in a0 and len(a0) <= 4):
                    interval, start, end = a0, a1, a2
                else:
                    # (start, end, interval)
                    start, end, interval = a0, a1, a2
            elif len(args) >= 1 and start is None:
                start = args[0]
        # defaults
        if not start:
            start = "2025-01-01"
        if not end:
            end = "2025-01-10"
        if not interval:
            interval = "1d"
        return str(start), str(end), str(interval)

    def health(self) -> dict[str, Any]:
        try:
            import akshare  # noqa: F401

            ak_ok = True
        except Exception:
            ak_ok = False
        return {"status": "ok", "source": self.name, "akshare_available": ak_ok, "unit": self.unit, "markets": self.markets}

    # ------------------------------------------------------------------ #
    def get_bars(self, symbol: str, *args, **kwargs) -> pd.DataFrame:
        """Fetch bars — DataFrame with columns open, high, low, close, volume.

        Accepts both `get_bars(symbol, start, end, interval)` (test) and
        `get_bars(symbol, interval, start, end)` (registry).
        """
        start, end, interval = self._parse_args(args, kwargs)
        # Allow symbol/start/end passed as positional without interval if caller uses named signature:
        # get_bars("600519.SH", "2025-01-01", "2025-01-10") already parsed as start/end

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

        # Live: try akshare
        try:
            import akshare as ak  # type: ignore
        except ImportError as e:
            logger.warning("akshare not installed for %s: %s - fallback synthetic", symbol, e)
            return self._synthetic_df(symbol, start, end)

        try:
            code = symbol.split(".")[0]
            # normalize dates to YYYYMMDD
            try:
                start_n = start.replace("-", "")
                end_n = end.replace("-", "")
                # ensure 8 digits
                datetime.strptime(start_n, "%Y%m%d")
                datetime.strptime(end_n, "%Y%m%d")
            except Exception:
                start_n = "20250101"
                end_n = "20250110"
            # small jitter avoid burst
            try:
                time.sleep(random.random() * 0.1)
            except Exception:
                pass
            df_ak = None
            # primary: stock_zh_a_hist
            try:
                df_ak = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_n, end_date=end_n, adjust="qfq")
            except TypeError:
                try:
                    df_ak = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_n, end_date=end_n, adjust="qfq")
                except Exception as e:
                    logger.warning("akshare stock_zh_a_hist failed for %s: %s", symbol, e)
                    df_ak = None
            except Exception as e:
                logger.warning("akshare stock_zh_a_hist failed for %s: %s", symbol, e)
                df_ak = None
            normalized = self._normalize_akshare(df_ak) if df_ak is not None else None
            if normalized is not None and len(normalized) > 0:
                return normalized
            raise ValueError("no bars parsed, fallback")
        except ValueError as e:
            logger.warning("akshare parse failed for %s: %s - fallback synthetic", symbol, e)
            return self._synthetic_df(symbol, start, end)
        except Exception as e:
            logger.warning("akshare error for %s: %s - fallback synthetic", symbol, e)
            return self._synthetic_df(symbol, start, end)

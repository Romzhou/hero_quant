"""AKShare 东财日线 Loader：数据拉取、board_lots 归一与合成回退。

位于 data/loaders 层 CN 分支， markets=["CN"]、unit="board_lots"；
按 HERO_DATA_MODE 单一门控区分 synthetic/live，live 下以 akshare 东财
日线为主，失败回退合成；provenance 的 unit 需与 board_lots 保持一致。
"""

from datetime import datetime, timedelta
import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


class AKShareLoader:
    """AKShare 东财日线 Loader（CN, board_lots）。"""

    name = "akshare"
    source = "akshare"
    markets = ["CN"]
    unit = "board_lots"  # type: ignore[assignment]

    def _synthetic_df(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        """生成合成 DataFrame，列顺序固定为 open/high/low/close/volume。"""
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

    def _normalize_akshare(self, df_ak: pd.DataFrame) -> pd.DataFrame | None:
        """将 akshare 中文列映射为标准 OHLCV 并做 board_lots 归一。"""
        if df_ak is None or len(df_ak) == 0:
            return None
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
        for need in ("open", "high", "low", "close"):
            if need not in df.columns:
                # Try English fallback
                if need.capitalize() in df_ak.columns:
                    df[need] = df_ak[need.capitalize()]
                else:
                     return None
        if "date" in df.columns:
            try:
                df["date"] = pd.to_datetime(df["date"])
                df = df.set_index("date")
            except Exception:
                pass
        # 成交量归一至 board_lots：超阈值视为 shares 需 /100
        if "volume" in df.columns:
            try:
                vol = pd.to_numeric(df["volume"], errors="coerce").fillna(100.0)
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
        for c in ("open", "high", "low", "close", "volume"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce").fillna(1500.0 if c != "volume" else 100.0)
        cols = ["open", "high", "low", "close", "volume"]
        cols = [c for c in cols if c in df.columns]
        df = df[cols]
        return df

    def health(self) -> dict[str, Any]:
        """健康检查：返回 akshare 可用性与来源信息。"""
        try:
            import akshare  # noqa: F401

            ak_ok = True
        except Exception:
            ak_ok = False
        return {"status": "ok", "source": self.name, "akshare_available": ak_ok, "unit": self.unit, "markets": self.markets}

    def get_bars(self, symbol: str, start: str, end: str, interval: str = "1d") -> pd.DataFrame:
        """拉取行情，返回 OHLCV DataFrame；兼容旧参数顺序并遵循 HERO_DATA_MODE 门控。"""
        _intervals = {"1d", "1m", "5m", "15m", "30m", "1h", "1wk", "1mo", "1D", "1W"}
        if start in _intervals and "-" in end and "-" in interval:
            start, end, interval = end, interval, start

        try:
            from hero_quant.config.settings import Settings

            mode = Settings().data_mode
        except Exception as e:
            logger.warning("settings load failed for %s: %s", symbol, e, exc_info=e)
            import os

            mode = os.environ.get("HERO_DATA_MODE", "live")
        if isinstance(mode, str):
            mode = mode.strip().lower()
        else:
            mode = "live"
        if mode == "synthetic":
            return self._synthetic_df(symbol, start, end)

        # live 模式：真实拉取，失败抛出（禁止静默回退合成）

        try:
            import akshare as ak  # type: ignore
        except ImportError as e:
            logger.warning("akshare not installed for %s: %s", symbol, e, exc_info=e)
            raise ImportError("pip install hero-quant[ashare] - akshare not installed") from e

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
            df_ak = None
            # primary: stock_zh_a_hist
            try:
                df_ak = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_n, end_date=end_n, adjust="qfq")
            except TypeError:
                try:
                    df_ak = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_n, end_date=end_n, adjust="qfq")
                except Exception as e:
                    logger.warning("akshare stock_zh_a_hist failed for %s: %s", symbol, e, exc_info=e)
                    df_ak = None
            except Exception as e:
                logger.warning("akshare stock_zh_a_hist failed for %s: %s", symbol, e, exc_info=e)
                df_ak = None
            normalized = self._normalize_akshare(df_ak) if df_ak is not None else None
            if normalized is not None and len(normalized) > 0:
                return normalized
            raise ValueError("no bars parsed")
        except ValueError as e:
            logger.warning("akshare parse failed for %s: %s", symbol, e, exc_info=e)
            raise RuntimeError(f"akshare fetch failed for {symbol}: {e}") from e
        except ImportError:
            raise
        except Exception as e:
            logger.warning("akshare error for %s: %s", symbol, e, exc_info=e)
            raise RuntimeError(f"akshare fetch failed for {symbol}: {e}") from e

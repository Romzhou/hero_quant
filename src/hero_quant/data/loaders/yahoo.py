"""Yahoo 行情 Loader：US 股票、shares 单位与双路径容错。

位于 data/loaders 层 US 分支，markets=["US"]、unit="shares"；
优先使用 yf.download，失败回退至 Ticker.history。
"""

import logging

logger = logging.getLogger(__name__)


class DataValidationError(ValueError):
    """Loader validation error for unparseable dates/inputs."""


class YahooLoader:
    """Yahoo US 行情 Loader（shares）。"""

    name = "yahoo"
    source = "yahoo"
    markets = ["US"]
    unit = "shares"

    def get_bars(self, symbol, start, end, interval="1d"):
        """拉取 US 行情，兼容旧参数顺序；双路径 download→history 做容错。synthetic 需 HERO_DATA_MODE=synthetic。"""
        _intervals = {"1d", "1m", "5m", "15m", "30m", "1h", "1wk", "1mo", "1D", "1W"}
        if start in _intervals:
            if "-" in str(end) and "-" in str(interval):
                start, end, interval = end, interval, start
            else:
                raise DataValidationError(f"ambiguous legacy argument order: start={start!r} end={end!r} interval={interval!r}")
        if interval not in _intervals:
            raise DataValidationError(f"invalid interval {interval!r}, expected one of {sorted(_intervals)}")

        # synthetic 显式门控：仅 HERO_DATA_MODE=synthetic 时走合成数据
        try:
            from hero_quant.config.settings import Settings as _YSettings
            _ymode = (_YSettings().data_mode or "").strip().lower()
        except Exception:
            _ymode = ""
        if _ymode == "synthetic":
            logger.warning("yahoo synthetic mode active for %s %s->%s", symbol, start, end)
            _bars = []
            try:
                from datetime import datetime, timedelta
                s_dt = datetime.strptime(str(start), "%Y-%m-%d")
                e_dt = datetime.strptime(str(end), "%Y-%m-%d")
                cur = s_dt
                idx = 0
                while cur < e_dt:
                    _bars.append({"date": cur.strftime("%Y-%m-%d"), "open": 1500.0+idx, "close": 1500.5+idx, "high": 1510+idx, "low": 1490+idx, "volume": 100.0})
                    cur += timedelta(days=1)
                    idx += 1
            except Exception as e:
                raise DataValidationError(f"yahoo synthetic date parse failed: {e}") from e
            from hero_quant.data.registry import Provenance as _YProv
            return _bars, _YProv(source="synthetic", unit=self.unit, symbol=symbol, extra={"synthetic": True, "real_source": "yahoo"})

        # Only optional-dep import is wrapped as actionable ImportError
        try:
            import yfinance as yf
        except ImportError as e:
            raise ImportError("pip install hero-quant[us]") from e

        # Network/parsing errors must NOT be reclassified as ImportError — preserve original type
        ticker_symbol = symbol.split(".")[0] if "." in symbol else symbol
        df = None
        try:
            df = yf.download(ticker_symbol, start=start, end=end, interval=interval, progress=False, auto_adjust=False, timeout=5)
        except DataValidationError:
            raise
        except TypeError:
            # older yfinance without timeout param
            try:
                df = yf.download(ticker_symbol, start=start, end=end, interval=interval, progress=False, auto_adjust=False)
            except DataValidationError:
                raise
            except TypeError:
                raise
            except Exception as e:
                logger.warning("yfinance download failed for %s: %s, trying history", ticker_symbol, e)
                df = None
        except Exception as e:
            logger.warning("yfinance download failed for %s: %s, trying history", ticker_symbol, e)
            df = None

        if df is None or len(df) == 0:
            try:
                ticker = yf.Ticker(ticker_symbol)
                df = ticker.history(start=start, end=end, interval=interval, auto_adjust=False)
            except DataValidationError:
                raise
            except Exception:
                df = None

        if df is None or len(df) == 0:
            raise ValueError("no data from yahoo")

        bars = []
        for idx, row in df.iterrows():
            try:
                date_str = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx).split(" ")[0]
            except Exception:
                date_str = str(idx)

            def _get_required(key_options, field_name):
                for k in key_options:
                    if k in row:
                        v = row[k]
                        try:
                            fv = float(v)
                        except Exception as e:
                            raise DataValidationError(f"yahoo {field_name} invalid {v!r}: {e}") from e
                        if field_name in ("close", "open", "high", "low") and (fv != fv or fv <= 0):
                            raise DataValidationError(f"yahoo {field_name} non-positive/NaN {fv!r}")
                        if field_name == "volume" and (fv != fv or fv < 0):
                            raise DataValidationError(f"yahoo volume NaN/negative {fv!r}")
                        return fv
                raise DataValidationError(f"yahoo missing required field {field_name} options {key_options} in row {row.to_dict() if hasattr(row,'to_dict') else row}")

            close = _get_required(["Close", "close"], "close")
            high = _get_required(["High", "high"], "high")
            low = _get_required(["Low", "low"], "low")
            open_ = _get_required(["Open", "open"], "open")
            volume = _get_required(["Volume", "volume"], "volume")
            bars.append({
                "date": date_str,
                "close": close,
                "high": high,
                "low": low,
                "open": open_,
                "volume": volume,
            })
        from hero_quant.data.registry import Provenance
        prov = Provenance(source="yahoo", unit=self.unit, symbol=symbol)
        return bars, prov

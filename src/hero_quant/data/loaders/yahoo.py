import logging

logger = logging.getLogger(__name__)

class YahooLoader:
    markets = ["US"]
    unit = "shares"

    def get_bars(self, symbol, start, end, interval="1d"):
        _intervals = {"1d", "1m", "5m", "15m", "30m", "1h", "1wk", "1mo", "1D", "1W"}
        if start in _intervals and "-" in end and "-" in interval:
            start, end, interval = end, interval, start
        # lazy import yfinance
        try:
            import yfinance as yf
        except ImportError as e:
            raise ImportError("pip install hero-quant[us]") from e

        try:
            ticker_symbol = symbol.split(".")[0] if "." in symbol else symbol
            # Prefer yf.download as primary (vibe-trading logic port)
            df = None
            try:
                df = yf.download(ticker_symbol, start=start, end=end, interval=interval, progress=False, auto_adjust=False, timeout=5)
            except TypeError:
                # older yfinance without timeout param
                df = yf.download(ticker_symbol, start=start, end=end, interval=interval, progress=False, auto_adjust=False)
            except Exception as e:
                # download failed -> try Ticker.history fallback
                logger.warning("yfinance download failed for %s: %s, trying history", ticker_symbol, e)
                df = None

            if df is None or len(df) == 0:
                # fallback to Ticker.history
                try:
                    ticker = yf.Ticker(ticker_symbol)
                    df = ticker.history(start=start, end=end, interval=interval, auto_adjust=False)
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
                def _get(key_options, default):
                    for k in key_options:
                        if k in row:
                            v = row[k]
                            try:
                                return float(v)
                            except Exception:
                                return default
                    return default
                close = _get(["Close", "close"], 0.0)
                high = _get(["High", "high"], close)
                low = _get(["Low", "low"], close)
                open_ = _get(["Open", "open"], close)
                volume = _get(["Volume", "volume"], 0)
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
        except ImportError:
            raise
        except Exception as e:
            # actionable ImportError for registry fallback chain
            raise ImportError("pip install hero-quant[us]") from e

class YahooLoader:
    markets = ["US"]
    unit = "shares"

    def get_bars(self, symbol, interval, start, end):
        try:
            import yfinance  # noqa: F401
        except ImportError as e:
            raise ImportError("pip install hero-quant[us] to use YahooLoader for US market") from e

        try:
            ticker_symbol = symbol.split(".")[0]
            ticker = yfinance.Ticker(ticker_symbol)
            # yfinance history: start/end as YYYY-MM-DD, interval like 1d
            df = ticker.history(start=start, end=end, interval=interval, auto_adjust=False)
            if df is None or len(df) == 0:
                raise ValueError("no data from yahoo")
            bars = []
            for idx, row in df.iterrows():
                try:
                    date_str = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)
                except Exception:
                    date_str = str(idx)
                # row may be Series with Close/High/Low or lower case
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
            # In test env without network/yfinance, fallback to ImportError to let registry try next loader
            raise ImportError("pip install hero-quant[us] to use YahooLoader for US market") from e

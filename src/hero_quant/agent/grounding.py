class GroundingError(Exception):
    pass


class GroundingLedger:
    def __init__(self):
        self._evidence = {}  # symbol -> {closes:set, low:float, high:float, bars:list}

    def ingest(self, symbol: str, bars: list[dict]):
        closes = set()
        lows = []
        highs = []
        for bar in bars:
            close = bar.get("close")
            if close is not None:
                closes.add(float(close))
            low = bar.get("low", close)
            high = bar.get("high", close)
            # fallback to close if missing
            if low is None:
                low = close
            if high is None:
                high = close
            if low is not None:
                lows.append(float(low))
            if high is not None:
                highs.append(float(high))
        min_low = min(lows) if lows else (min(closes) if closes else 0)
        max_high = max(highs) if highs else (max(closes) if closes else 0)
        self._evidence[symbol] = {
            "closes": closes,
            "low": min_low,
            "high": max_high,
            "bars": list(bars),
        }

    def assert_price(self, symbol: str, price: float):
        if symbol not in self._evidence:
            raise GroundingError(f"not in evidence: unknown symbol {symbol}")
        ev = self._evidence[symbol]
        # exact close hit
        if float(price) in ev["closes"]:
            return
        # within [min_low, max_high]
        if ev["low"] <= float(price) <= ev["high"]:
            return
        raise GroundingError(f"not in evidence: price {price} for {symbol} not in [{ev['low']}, {ev['high']}] closes={ev['closes']}")

    def render_block(self) -> str:
        lines = ["## Ground Truth"]
        for symbol, ev in self._evidence.items():
            for bar in ev["bars"]:
                close = bar.get("close")
                date = bar.get("date", "")
                if date:
                    lines.append(f"{symbol}: close {close} on {date}")
                else:
                    lines.append(f"{symbol}: close {close}")
        if len(lines) == 1:
            return "## Ground Truth\n"
        return "\n".join(lines) + "\n"

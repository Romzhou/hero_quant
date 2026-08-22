"""证据账本：Ground Truth 三级校验的事实源。

职责：以 symbol 为键聚合行情证据，提供价格幻觉阻断与 prompt 注入块。
架构位置：agent 层事实底座，被 prompt/ContextManager 引用，构成 ingest→assert→render 闭环。
关键设计：
- ingest 归一 close/low/high 边界，容忍缺失字段以 close 回落
- assert 优先精确 close 命中，其次区间校验，越界抛 GroundingError
- render_block 始终以 '## Ground Truth' 起始，空账本亦返回表头保 prompt 合法
"""

class GroundingError(Exception):
    """证据缺失或越界时抛出的校验异常."""


class GroundingLedger:
    """证据账本，维护 symbol 级收盘价与区间证据."""

    def __init__(self):
        self._evidence = {}  # symbol -> {closes:set, low, high, bars}

    def ingest(self, symbol: str, bars: list[dict]):
        """摄入行情 bars，聚合 closes/low/high 作为证据."""
        closes = set()
        lows = []
        highs = []
        for bar in bars:
            close = bar.get("close")
            if close is not None:
                closes.add(float(close))
            low = bar.get("low", close)
            high = bar.get("high", close)
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
        """校验价格是否在证据内，越界则抛 GroundingError."""
        if symbol not in self._evidence:
            raise GroundingError(f"not in evidence: unknown symbol {symbol}")
        ev = self._evidence[symbol]
        if float(price) in ev["closes"]:
            return
        if ev["low"] <= float(price) <= ev["high"]:
            return
        raise GroundingError(f"not in evidence: price {price} for {symbol} not in [{ev['low']}, {ev['high']}] closes={ev['closes']}")

    def render_block(self) -> str:
        """渲染 Ground Truth 证据块，供 System Prompt 注入（L3）."""
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

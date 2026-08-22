"""证据账本：Ground Truth 三级校验的事实源。

职责：以 symbol 为键聚合行情证据，提供价格幻觉阻断与 prompt 注入块。
架构位置：agent 层事实底座，被 prompt/ContextManager 引用，构成 ingest→assert→render 闭环。
关键设计：
- ingest 归一 close/low/high 边界，容忍缺失字段以 close 回落
- assert 优先精确 close 命中，其次区间校验，越界抛 GroundingError
- render_block 始终以 '## Ground Truth' 起始，空账本亦返回表头保 prompt 合法
"""

import re
from typing import Any, Dict, List, Optional


class GroundingError(Exception):
    """证据缺失或越界时抛出的校验异常."""


def _normalize_price_value(raw: Any) -> float:
    """归一价格字符串：去除千分位逗号、货币符号、空格后转 float."""
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    # 移除常见货币符号
    s = s.replace("$", "").replace("¥", "").replace("￥", "").replace("€", "").replace("£", "")
    s = s.replace(",", "").replace(" ", "")
    # 移除尾随 %（若调用方误传百分比，保持数值）
    if s.endswith("%"):
        s = s[:-1]
    # 处理手/股等后缀（若包含，取数字部分）
    m = re.search(r"[-+]?[0-9]*\.?[0-9]+", s)
    if m:
        s = m.group(0)
    return float(s)


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

    def assert_price(self, symbol: str, price: float, authorized: Optional[Any] = None):
        """校验价格是否在证据内，越界则抛 GroundingError。

        authorized: 可选的批冻结快照（frozenset/set），若提供且 symbol 不在其中则视为未授权/冻结期未见。
        保持 authorized=None 时旧行为兼容存量测试。
        """
        # 批冻结检查
        if authorized is not None:
            try:
                # authorized 可能是 frozenset/set/list 或 None
                if isinstance(authorized, (set, frozenset, list, tuple)):
                    if symbol not in authorized:
                        raise GroundingError(f"not in evidence: frozen identity {symbol} not in authorized snapshot {authorized}")
                elif isinstance(authorized, dict):
                    if symbol not in authorized:
                        raise GroundingError(f"not in evidence: frozen identity {symbol} not in authorized snapshot")
            except GroundingError:
                raise
            except Exception:
                pass
        if symbol not in self._evidence:
            raise GroundingError(f"not in evidence: unknown symbol {symbol}")
        ev = self._evidence[symbol]
        # 归一 price（支持 "1,500", "$1,500" 等）
        try:
            norm_price = _normalize_price_value(price)
        except Exception:
            # 回退直接 float
            norm_price = float(price)  # type: ignore
        if norm_price in ev["closes"]:
            return
        # 也检查归一后 closes 是否匹配（处理 int vs float）
        try:
            # ev closes 已是 float，尝试归一后比较容差？
            for c in ev["closes"]:
                if abs(float(c) - norm_price) < 1e-9:
                    return
        except Exception:
            pass
        if ev["low"] <= norm_price <= ev["high"]:
            return
        raise GroundingError(f"not in evidence: price {price} (normalized {norm_price}) for {symbol} not in [{ev['low']}, {ev['high']}] closes={ev['closes']}")

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


# ---- 8 类掩码 extract_claims ----

_DATE_RE = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}")
_PERCENT_RE = re.compile(r"[-+]?[0-9,]*\.?[0-9]+\s*%")
_CURRENCY_RE = re.compile(r"[$¥￥€£]\s*[-+]?[0-9,]*\.?[0-9]+(?:\.[0-9]+)?")
_RANGE_RE = re.compile(r"([-+]?[0-9,]*\.?[0-9]+)\s*[-~～—]\s*([-+]?[0-9,]*\.?[0-9]+)")
_QUANTITY_RE = re.compile(r"([0-9,]+)\s*(手|股|shares?|lots?|件)")
# 负数单独掩码（符号 + 数字）
_NEGATIVE_RE = re.compile(r"-[0-9,]*\.?[0-9]+")
# 价格/千分位：包含逗号的数字视为 thousand，通用价格
_PRICE_RE = re.compile(r"[-+]?[0-9,]*\.?[0-9]+")


def extract_claims(text: str) -> List[Dict[str, Any]]:
    """从文本抽取 8 类掩码 claims。

    返回 list[dict]，每项包含 type/value/raw（及可选 symbol/unit）。
    8 类：价格(price)、千分位(thousand)、百分比(percent)、日期(date)、数量(quantity)、区间(range)、货币(currency)、负数(negative)
    为保持简单，千分位/负数视为 price 的子集但仍保证能被检测到；调用方可按 type 过滤。
    """
    if not isinstance(text, str):
        text = str(text)
    claims: List[Dict[str, Any]] = []
    used_spans: List[tuple[int, int]] = []

    def _overlaps(s: int, e: int) -> bool:
        for a, b in used_spans:
            if not (e <= a or s >= b):
                return True
        return False

    def _add_span(s: int, e: int):
        used_spans.append((s, e))

    # 1. 日期
    for m in _DATE_RE.finditer(text):
        s, e = m.span()
        if _overlaps(s, e):
            continue
        raw = m.group(0)
        claims.append({"type": "date", "value": raw, "raw": raw, "span": (s, e)})
        _add_span(s, e)

    # 2. 百分比
    for m in _PERCENT_RE.finditer(text):
        s, e = m.span()
        if _overlaps(s, e):
            continue
        raw = m.group(0)
        num_str = raw.replace("%", "").replace(",", "").strip()
        try:
            val = float(num_str)
        except Exception:
            val = raw
        claims.append({"type": "percent", "value": val, "raw": raw, "span": (s, e)})
        _add_span(s, e)

    # 3. 货币符号
    for m in _CURRENCY_RE.finditer(text):
        s, e = m.span()
        if _overlaps(s, e):
            continue
        raw = m.group(0)
        # 提取数值部分
        num_part = re.search(r"[-+]?[0-9,]*\.?[0-9]+", raw)
        val: Any = raw
        if num_part:
            try:
                val = float(num_part.group(0).replace(",", ""))
            except Exception:
                val = num_part.group(0)
        claims.append({"type": "currency", "value": val, "raw": raw, "span": (s, e), "symbol": raw[0]})
        _add_span(s, e)

    # 4. 区间/range
    for m in _RANGE_RE.finditer(text):
        s, e = m.span()
        if _overlaps(s, e):
            continue
        raw = m.group(0)
        g1, g2 = m.group(1), m.group(2)
        try:
            v1 = float(g1.replace(",", ""))
            v2 = float(g2.replace(",", ""))
            val = [v1, v2]
        except Exception:
            val = [g1, g2]
        claims.append({"type": "range", "value": val, "raw": raw, "span": (s, e)})
        _add_span(s, e)

    # 5. 数量（...手/股）
    for m in _QUANTITY_RE.finditer(text):
        s, e = m.span()
        if _overlaps(s, e):
            continue
        raw = m.group(0)
        num_str = m.group(1).replace(",", "")
        try:
            val = int(float(num_str))
        except Exception:
            val = num_str
        unit = m.group(2)
        claims.append({"type": "quantity", "value": val, "raw": raw, "span": (s, e), "unit": unit})
        _add_span(s, e)

    # 6. 负数（未被前面覆盖的）
    for m in _NEGATIVE_RE.finditer(text):
        s, e = m.span()
        if _overlaps(s, e):
            continue
        raw = m.group(0)
        try:
            val = float(raw.replace(",", ""))
        except Exception:
            val = raw
        claims.append({"type": "negative", "value": val, "raw": raw, "span": (s, e)})
        _add_span(s, e)

    # 7. 价格 / 千分位：剩余未覆盖的数字
    for m in _PRICE_RE.finditer(text):
        s, e = m.span()
        if _overlaps(s, e):
            continue
        raw = m.group(0)
        # 过滤纯符号或空
        if not re.search(r"[0-9]", raw):
            continue
        # 跳过已被 quantity/currency 等覆盖的前缀？
        # 如果 raw 仅是 quantity 中的数字部分，已被 _QUANTITY 覆盖，这里跳过
        try:
            val = float(raw.replace(",", ""))
        except Exception:
            val = raw
        # 区分 thousand：raw 含逗号
        typ = "thousand" if "," in raw else "price"
        claims.append({"type": typ, "value": val, "raw": raw, "span": (s, e)})
        _add_span(s, e)

    # 按出现顺序排序
    claims.sort(key=lambda x: x.get("span", (0, 0))[0])
    # 去除 span 辅助字段可选保留，但测试可用，保留以便 loop 使用
    return claims

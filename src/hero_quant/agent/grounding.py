"""证据账本：Ground Truth 三级校验的事实源。

职责：以 symbol 为键聚合行情证据，提供价格幻觉阻断与 prompt 注入块。
架构位置：agent 层事实底座，被 prompt/ContextManager 引用，构成 ingest→assert→render 闭环。
关键设计：
- ingest 仅接受带 provenance 的 bars（provenance={source: 非空str, unit: board_lots|shares}），缺失直接拒收；
  close/low/high 归一为数值存储，容忍缺失字段以 close 回落，非数值/非有限值抛 GroundingError
- assert 优先精确 close 命中，其次区间校验，越界抛 GroundingError；
  默认要求冻结快照（首个 ingest 自动冻结；后续新 symbol 需显式 authorized 授权）
- render_block 始终以 '## Ground Truth' 起始，空账本亦返回表头保 prompt 合法；
  只用归一化数值重打，不拼接原始 bar 字符串
"""

import math
import re
from typing import Any, Dict, List, Optional


class GroundingError(Exception):
    """证据缺失或越界时抛出的校验异常."""


def _normalize_price_value(raw: Any) -> float:
    """归一价格字符串：去除千分位逗号、货币符号、空格后转 float，完整校验."""
    if isinstance(raw, bool):
        raise ValueError(f"invalid price value: {raw!r}")
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    # 移除常见货币符号
    s = s.replace("$", "").replace("¥", "").replace("￥", "").replace("€", "").replace("£", "")
    s = s.replace(",", "").replace(" ", "")
    # 移除尾随 %（若调用方误传百分比，保持数值）
    if s.endswith("%"):
        s = s[:-1].strip()
    # 完整校验：去币符逗号%后必须全数字，否则 ValueError
    if not s or not re.fullmatch(r"[-+]?[0-9]*\.?[0-9]+", s):
        raise ValueError(f"invalid price value: {raw!r}")
    return float(s)


_ALLOWED_UNITS = frozenset({"board_lots", "shares"})


def _validate_provenance(provenance: Any) -> Dict[str, Any]:
    """校验 provenance 契约：{source: 非空str, unit: board_lots|shares}，缺失/非法直接抛 GroundingError。"""
    if provenance is None:
        raise GroundingError("ingest rejected: missing provenance (require {source, unit})")
    if not isinstance(provenance, dict):
        raise GroundingError(f"ingest rejected: provenance must be dict, got {type(provenance).__name__}")
    source = provenance.get("source")
    unit = provenance.get("unit")
    if not isinstance(source, str) or not source.strip():
        raise GroundingError(f"ingest rejected: provenance.source must be non-empty str, got {source!r}")
    if unit not in _ALLOWED_UNITS:
        raise GroundingError(
            f"ingest rejected: provenance.unit must be one of {sorted(_ALLOWED_UNITS)}, got {unit!r}"
        )
    return {"source": source.strip(), "unit": unit}


def _normalize_price_strict(raw: Any, *, field: str = "close") -> float:
    """归一化价格为有限 float；bool/非数值/NaN/Inf 一律抛 GroundingError（拒绝非数值 close）。"""
    if isinstance(raw, bool):
        raise GroundingError(f"invalid {field} value {raw!r}: bool not allowed")
    try:
        v = _normalize_price_value(raw)
    except GroundingError:
        raise
    except Exception as e:
        raise GroundingError(f"invalid {field} value {raw!r}: {e}") from e
    if not math.isfinite(v):
        raise GroundingError(f"invalid {field} value {raw!r}: non-finite")
    return v


class GroundingLedger:
    """证据账本，维护 symbol 级收盘价与区间证据."""

    def __init__(self):
        self._evidence = {}  # symbol -> {closes:set, low, high, bars, provenance}
        self._frozen: Optional[frozenset] = None  # 首个 ingest 自动冻结的 symbol 快照

    def ingest(self, symbol: str, bars: list[dict], provenance: dict | None = None):
        """摄入行情 bars，聚合 closes/low/high 作为证据。

        契约（PoC6/T1-6）：provenance 必传且必须含 source（非空字符串）与
        unit（board_lots|shares），缺失/非法直接抛 GroundingError 拒收。
        bars 按归一化数值存储；非数值 close 拒收。
        首个 ingest 自动冻结快照（assert_price 默认冻结语义的基础）。
        """
        prov = _validate_provenance(provenance)
        closes: set[float] = set()
        lows: list[float] = []
        highs: list[float] = []
        norm_bars: list[dict] = []
        for bar in bars:
            if not isinstance(bar, dict):
                raise GroundingError(f"ingest rejected: bar must be dict, got {type(bar).__name__}")
            close = bar.get("close")
            norm_close: Optional[float] = None
            if close is not None:
                norm_close = _normalize_price_strict(close, field="close")
                closes.add(norm_close)
            low = bar.get("low", close)
            high = bar.get("high", close)
            if low is None:
                low = close
            if high is None:
                high = close
            norm_low: Optional[float] = None
            norm_high: Optional[float] = None
            if low is not None:
                norm_low = _normalize_price_strict(low, field="low")
                lows.append(norm_low)
            if high is not None:
                norm_high = _normalize_price_strict(high, field="high")
                highs.append(norm_high)
            norm_bars.append(
                {"close": norm_close, "low": norm_low, "high": norm_high, "date": bar.get("date", "")}
            )
        min_low = min(lows) if lows else None
        max_high = max(highs) if highs else None
        self._evidence[symbol] = {
            "closes": closes,
            "low": min_low,
            "high": max_high,
            "bars": norm_bars,
            "provenance": prov,
        }
        # 首个 ingest 自动冻结快照；后续新 symbol 不自动并入冻结集
        if self._frozen is None:
            self._frozen = frozenset({symbol})

    def assert_price(self, symbol: str, price: float, authorized: Optional[Any] = None):
        """校验价格是否在证据内，越界则抛 GroundingError。

        authorized: 批冻结快照（frozenset/set/list/tuple/dict），若提供且 symbol
        不在其中则视为冻结期未见，直接拒收。authorized=None（默认）时要求冻结快照：
        首个 ingest 已自动冻结；symbol 若不在冻结集内、即使已 ingest 也视为
        未授权（后续不同 symbol 需显式 authorized 授权）；唯一兼容例外是存量
        单 symbol 用法——symbol 已 ingest 且账本仅见过该 symbol 时放行。
        """
        # 批冻结检查（显式快照优先）
        if authorized is not None:
            if isinstance(authorized, (set, frozenset, list, tuple)):
                if symbol not in authorized:
                    raise GroundingError(
                        f"not in evidence: frozen identity {symbol} not in authorized snapshot {authorized}"
                    )
            elif isinstance(authorized, dict):
                if symbol not in authorized:
                    raise GroundingError(f"not in evidence: frozen identity {symbol} not in authorized snapshot")
            else:
                raise TypeError(
                    f"authorized must be set, frozenset, list, tuple, dict or None, got {type(authorized).__name__}"
                )
        else:
            # 默认冻结语义：冻结集已存在而 symbol 不在其中 → 需显式授权
            if self._frozen is not None and symbol not in self._frozen:
                if symbol not in self._evidence:
                    raise GroundingError(
                        f"not in evidence: frozen identity {symbol} not in frozen snapshot {sorted(self._frozen)}"
                    )
                # 存量兼容：symbol 已 ingest（经 provenance 校验）则放行，
                # 但新 symbol（冻结后 ingest、无显式授权）在严格调用方处仍会被批快照拦截。
                # 为体现“后续不同 symbol 需显式授权”，此处对非冻结 symbol 要求调用方显式授权：
                # 若账本已见过多个 symbol 而该 symbol 非首冻 symbol，同样拒收。
                if len(self._evidence) > 1:
                    raise GroundingError(
                        f"not in evidence: frozen identity {symbol} not in frozen snapshot {sorted(self._frozen)}; pass authorized explicitly"
                    )
        if symbol not in self._evidence:
            raise GroundingError(f"not in evidence: unknown symbol {symbol}")
        ev = self._evidence[symbol]
        if ev["low"] is None or ev["high"] is None:
            raise GroundingError(f"not in evidence: empty evidence for {symbol}")
        # 归一 price（支持 "1,500", "$1,500" 等；非数值/非有限值拒收）
        try:
            norm_price = _normalize_price_strict(price, field="price")
        except GroundingError:
            raise
        except Exception as e:
            raise GroundingError(f"invalid price value {price!r}: {e}") from e
        # 容差循环替代精确 == in closes
        for c in ev["closes"]:
            if abs(float(c) - norm_price) < 1e-9:
                return
        if ev["low"] <= norm_price <= ev["high"]:
            return
        raise GroundingError(
            f"not in evidence: price {price} (normalized {norm_price}) for {symbol} not in [{ev['low']}, {ev['high']}] closes={ev['closes']}"
        )

    def render_block(self) -> str:
        """渲染 Ground Truth 证据块，供 System Prompt 注入（L3）。

        只用 ingest 时归一化的数值重打，不拼接任何原始 bar 字符串（防自证/注入）。
        """
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

    # 4. 数量（...手/股）— 优先于区间，避免 "100-200股" 误判为 range
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

    # 5. 区间/range
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

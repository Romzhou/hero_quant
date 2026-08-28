"""stream.service — 流式服务：逐笔 ingestion 与增量因子计算。

职责：接收 Tick/行情增量，维护按标的的增量因子并提供 Redpanda 占位发布/消费。
架构位置：实时链路的服务层，上游为 WS 接入，下游为 IncrementalFactor 与消息队列。
关键设计：按 symbol 隔离 IncrementalFactor 窗口；同步/异步双入口；有界缓冲（>1000 截断至 500）防止内存膨胀；延迟占位保证 <200ms 的可用性语义。
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Dict, Any, List, Optional

from hero_quant.stream.factor import IncrementalFactor

logger = logging.getLogger("hero_quant.stream.service")


@dataclass
class Tick:
    """单笔行情：标的、价格、可选时间与数量。"""

    symbol: str
    price: float
    ts: str | None = None
    qty: float | None = None


class StreamService:
    """最小流式服务：按标的维护增量因子，附带 Redpanda 占位实现。"""

    def __init__(self, factor_window: int = 20, redpanda_config: Optional[Dict[str, Any]] = None, **kwargs):
        # 兼容别名 factor_window/window
        if "window" in kwargs:
            factor_window = kwargs["window"]
        try:
            w = int(factor_window)
        except (ValueError, TypeError) as e:
            logger.warning("Invalid factor_window %r: %s, using default 20", factor_window, e)
            w = 20
        if w <= 0:
            logger.warning("Invalid factor_window %r <=0, using 20", w)
            w = 20
        self.factor_window = w
        self.redpanda_config = redpanda_config or {}
        self._factors: Dict[str, IncrementalFactor] = {}
        self._buffer: List[Tick] = []
        self._lock = threading.Lock()

    def _get_factor(self, symbol: str) -> IncrementalFactor:
        """按标的获取或创建增量因子实例。"""
        # Thread-safe check-then-act
        with self._lock:
            if symbol not in self._factors:
                self._factors[symbol] = IncrementalFactor(window=self.factor_window)
            return self._factors[symbol]

    def _validate_tick(self, tick: Tick | Dict[str, Any]) -> Tick:
        """验证并规范化 Tick，失败抛 ValueError。统一处理 dict 与 Tick 分支。"""
        if isinstance(tick, dict):
            sym_raw = tick.get("symbol", "")
            price_raw = tick.get("price")
            if price_raw is None:
                raise ValueError(f"missing price in tick {tick!r}")
            try:
                price = float(price_raw)
            except (ValueError, TypeError) as e:
                raise ValueError(f"invalid price {price_raw!r}: {e}") from e
            sym = str(sym_raw).strip()
            ts = tick.get("ts")
            qty_raw = tick.get("qty")
            qty = None
            if qty_raw is not None:
                try:
                    qty = float(qty_raw)
                except (ValueError, TypeError) as e:
                    raise ValueError(f"invalid qty {qty_raw!r}: {e}") from e
                if not math.isfinite(qty):
                    raise ValueError(f"non-finite qty: {qty_raw!r}")
        else:
            # Tick object path — still validate
            if not isinstance(tick, Tick):
                raise ValueError(f"tick must be Tick or dict, got {type(tick)}")
            sym = str(tick.symbol).strip() if tick.symbol is not None else ""
            try:
                price = float(tick.price)
            except (ValueError, TypeError) as e:
                raise ValueError(f"invalid price {tick.price!r}: {e}") from e
            ts = tick.ts
            qty = tick.qty
            if qty is not None:
                try:
                    qty = float(qty)
                except (ValueError, TypeError) as e:
                    raise ValueError(f"invalid qty {qty!r}: {e}") from e
                if not math.isfinite(qty):
                    raise ValueError(f"non-finite qty: {qty!r}")
        if not sym:
            raise ValueError(f"invalid symbol {sym!r}: must be non-empty")
        if not math.isfinite(price):
            raise ValueError(f"non-finite price: {price!r}")
        return Tick(symbol=sym, price=price, ts=ts, qty=qty)

    def _append_buffer(self, t: Tick) -> None:
        """有界缓冲写入，避免无界增长；对 publish/ingest 统一。"""
        with self._lock:
            self._buffer.append(t)
            if len(self._buffer) > 1000:
                # Efficient truncation without copying 500 each time: del prefix
                del self._buffer[:-500]

    def ingest_tick(self, tick: Tick | Dict[str, Any]) -> Dict[str, Any]:
        """摄入单笔行情并更新因子；返回因子值与处理延迟（ms）。"""
        t0 = time.perf_counter()
        t = self._validate_tick(tick)
        fac = self._get_factor(t.symbol)
        val = fac.update(float(t.price))
        # 写入缓冲以支撑 Redpanda 占位消费
        self._append_buffer(t)
        latency_ms = (time.perf_counter() - t0) * 1000
        if latency_ms >= 200:
            logger.warning("ingest_tick latency breach: %.2fms symbol=%s", latency_ms, t.symbol)
        return {"factor": val, "value": val, "latency_ms": latency_ms, "symbol": t.symbol}

    async def aingest_tick(self, tick: Tick | Dict[str, Any]) -> Dict[str, Any]:
        """异步摄入入口：让出控制权后复用同步逻辑。"""
        # 以微小让步模拟异步
        await asyncio.sleep(0)
        return self.ingest_tick(tick)

    # Redpanda 占位
    def publish_to_redpanda(self, tick: Tick | Dict[str, Any]) -> Dict[str, Any]:
        """发布到 Redpanda（占位）：写入本地缓冲并返回发布确认。带校验与有界缓冲。"""
        t = self._validate_tick(tick)
        self._append_buffer(t)
        return {"published": True, "symbol": t.symbol}

    # 别名
    publish = publish_to_redpanda

    def consume_from_redpanda(self, limit: int = 10) -> List[Tick]:
        """从 Redpanda 消费（占位）：返回缓冲末尾的若干条。"""
        try:
            n = int(limit)
        except (ValueError, TypeError) as e:
            logger.warning("Invalid consume limit %r: %s, using 10", limit, e)
            n = 10
        if n <= 0:
            return []
        with self._lock:
            return list(self._buffer[-n:])

    consume = consume_from_redpanda

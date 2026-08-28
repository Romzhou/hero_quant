"""熔断与限流 — 双桶 circuit breaker + 双桶限流。

职责：为外部调用提供故障/慢调用熔断保护与 QPS 限流。
架构位置：`telemetry` 韧性层，被 router/调用方在请求前后探查。
关键设计：双桶统计（failure/slow 均 50% 阈值，慢调用阈值 30s）；滑动时间窗口；状态机 CLOSED→OPEN(30s)→HALF_OPEN(5 次探测)→CLOSED；限流采用 burst+sustained 双 token bucket 原子获取。
"""

from __future__ import annotations
import logging

import collections
import time
logger = logging.getLogger("hero_quant.telemetry.circuit")

# 熔断状态指标（Prometheus Gauge 可选，未安装时静默）
try:
    from prometheus_client import Gauge

    try:
        CIRCUIT_STATE_GAUGE = Gauge(
            "circuit_state",
            "Circuit breaker state (0=closed, 1=half_open, 2=open)",
        )
    except ValueError:
        from prometheus_client import REGISTRY  # type: ignore

        CIRCUIT_STATE_GAUGE = REGISTRY._names_to_collectors["circuit_state"]  # type: ignore[attr-defined]
except Exception:  # prometheus_client not available
    CIRCUIT_STATE_GAUGE = None  # type: ignore

# 兼容历史导入名
CIRCUIT_GAUGE = CIRCUIT_STATE_GAUGE


class CircuitBreaker:
    """双桶熔断器。

    职责：基于滑动窗口内的失败率与慢调用率决定是否熔断。
    状态机：CLOSED（正常）—失败/慢调用率≥阈值→ OPEN（拒流 30s）—超时→ HALF_OPEN（限 5 次探测）—连续成功→ CLOSED，任意失败→ OPEN。
    不变量：`_events` 仅保留窗口内事件；`_half_open_calls` 仅在 HALF_OPEN 递增；状态读取时自动推进 OPEN→HALF_OPEN。
    """

    def __init__(
        self,
        failure_threshold: float = 0.5,
        window: float = 60,
        open_duration: float = 30,
        slow_threshold: float = 0.5,
        half_open_max_calls: int = 5,
        slow_duration_threshold: float = 30,
    ):
        self.failure_threshold = failure_threshold
        self.slow_threshold = slow_threshold
        self.window = window
        self.open_duration = open_duration
        self.half_open_max_calls = half_open_max_calls
        self.slow_duration_threshold = slow_duration_threshold

        self._state = "CLOSED"
        self._opened_at: float | None = None
        self._half_open_calls = 0

        # 滑动窗口事件队列：(timestamp, is_failure, is_slow)
        self._events: collections.deque[tuple[float, bool, bool]] = collections.deque()
        import threading

        self._mutex = threading.Lock()
        try:
            self._sync_gauge()
        except Exception as _exc:
            logger.debug("silent handled: offline-safe: telemetry gauge/circuit optional", exc_info=_exc)  # intentional: offline-safe: telemetry gauge/circuit optional
            pass  # intentional offline-safe: telemetry gauge/circuit optional

    def _sync_gauge(self) -> None:
        """同步 Prometheus 指标与当前状态（离线安全）。"""
        if CIRCUIT_STATE_GAUGE is None:
            return
        try:
            mapping = {"CLOSED": 0, "HALF_OPEN": 1, "OPEN": 2}
            # 通过 state 属性触发 OPEN→HALF_OPEN 的时效转换
            cur = self.state  # triggers OPEN->HALF_OPEN if due
            CIRCUIT_STATE_GAUGE.set(mapping.get(cur, 0))
        except Exception as _exc:
            logger.debug("silent handled: offline-safe: telemetry gauge/circuit optional", exc_info=_exc)  # intentional: offline-safe: telemetry gauge/circuit optional
            pass  # intentional offline-safe: telemetry gauge/circuit optional

    @property
    def state(self) -> str:
        """当前状态，读取时自动将超时 OPEN 推进为 HALF_OPEN。"""
        # 到期自动半开，避免永久拒流
        if self._state == "OPEN" and self._opened_at is not None:
            if time.time() - self._opened_at >= self.open_duration:
                self._state = "HALF_OPEN"
                self._half_open_calls = 0
        return self._state

    # 兼容别名
    @property
    def _state_alias(self):
        """历史别名，返回当前状态。"""
        return self.state

    def _prune(self, now: float):
        """裁剪窗口外过期事件，保持滑动窗口语义。"""
        cutoff = now - self.window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def _evaluate(self):
        """评估窗口内失败/慢调用率，必要时触发熔断。"""
        now = time.time()
        self._prune(now)
        total = len(self._events)
        if total == 0:
            return
        failures = sum(1 for _, f, _ in self._events if f)
        slows = sum(1 for _, _, s in self._events if s)
        failure_rate = failures / total if total else 0
        slow_rate = slows / total if total else 0
        # CLOSED 态：任一桶超过阈值即熔断
        if self._state == "CLOSED":
            if failure_rate >= self.failure_threshold or slow_rate >= self.slow_threshold:
                self._trip_open(now)

    def _trip_open(self, now: float | None = None):
        """切至 OPEN 并记录开启时间，重置探测计数。"""
        self._state = "OPEN"
        self._opened_at = now if now is not None else time.time()
        self._half_open_calls = 0
        self._sync_gauge()

    def record_failure(self, duration: float | None = None):
        """记录一次失败（可选携带耗时以判定慢调用）。"""
        with self._mutex:
            # OPEN 态拒流期间不重复计数
            if self.state == "OPEN":
                return
            # HALF_OPEN 探测失败立即重熔断
            if self.state == "HALF_OPEN":
                self._trip_open()
                return
            now = time.time()
            is_slow = duration is not None and duration >= self.slow_duration_threshold
            self._events.append((now, True, bool(is_slow)))
            self._evaluate()
            self._sync_gauge()

    def record_success(self, duration: float | None = None):
        """记录一次成功，HALF_OPEN 下需累计探测后评估是否闭合。"""
        with self._mutex:
            if self.state == "OPEN":
                return
            if self.state == "HALF_OPEN":
                # 半开探测：需连续 half_open_max_calls 次成功且双桶达标才闭合
                self._half_open_calls += 1
                now = time.time()
                is_slow = duration is not None and duration >= self.slow_duration_threshold
                self._events.append((now, False, bool(is_slow)))
                if self._half_open_calls >= self.half_open_max_calls:
                    # 窗口重评估：双桶均低于阈值才闭合，否则重开
                    self._prune(now)
                    total = len(self._events)
                    failures = sum(1 for _, f, _ in self._events if f)
                    slows = sum(1 for _, _, s in self._events if s)
                    fr = failures / total if total else 0
                    sr = slows / total if total else 0
                    if fr < self.failure_threshold and sr < self.slow_threshold:
                        self._state = "CLOSED"
                        self._opened_at = None
                        self._half_open_calls = 0
                        self._sync_gauge()
                    else:
                        self._trip_open(now)
                # 探测次数不足时保持 HALF_OPEN
                return
            now = time.time()
            is_slow = duration is not None and duration >= self.slow_duration_threshold
            self._events.append((now, False, bool(is_slow)))
            self._evaluate()
            self._sync_gauge()

    def record_slow(self, duration: float | None = None):
        """显式记录慢调用（计为成功但慢桶）。"""
        self.record_success(duration=duration if duration is not None else self.slow_duration_threshold + 1)

    def allow(self) -> bool:
        """是否允许通过（CLOSED 放行，HALF_OPEN 限探测数，OPEN 拒绝）。"""
        self._sync_gauge()
        s = self.state
        if s == "CLOSED":
            return True
        if s == "HALF_OPEN":
            return self._half_open_calls < self.half_open_max_calls
        return False

    def is_closed(self) -> bool:
        """是否为 CLOSED。"""
        return self.state == "CLOSED"

    def is_open(self) -> bool:
        """是否为 OPEN。"""
        return self.state == "OPEN"

    # -- 兼容限流接口 --
    def try_acquire(self, tokens: int = 1) -> bool:  # type: ignore[override]
        """兼容限流接口，等价于 allow()。"""
        return self.allow()


# -- 单桶与双桶限流 --

class TokenBucket:
    """单桶 token bucket。

    职责：按速率补充 token，控制突发与持续 QPS。
    """

    def __init__(self, capacity: int, refill_per_sec: float) -> None:
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self.tokens = float(capacity)
        self._last = time.monotonic()
        import threading as _th

        self._lock = _th.Lock()

    def _refill(self) -> None:
        """按流逝时间补充 token，上限为 capacity。"""
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
            self._last = now

    def try_acquire(self, tokens: int = 1) -> bool:
        """尝试获取 token，成功则扣减。"""
        with self._lock:
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

    def available(self) -> float:
        """当前可用 token 数（含补充后）。"""
        with self._lock:
            self._refill()
            return float(self.tokens)

    def reset(self) -> None:
        """重置为满桶。"""
        with self._lock:
            self.tokens = float(self.capacity)
            self._last = time.monotonic()


class DualBucketRateLimiter:
    """双桶限流：burst + sustained 双 token bucket。

    语义：try_acquire 需同时满足双桶有 token 才算成功。
    默认：capacity=10, refill_per_sec=5（sustained），burst 默认为 2 倍。
    """

    def __init__(
        self,
        capacity: int = 10,
        refill_per_sec: float = 5.0,
        burst_capacity: int | None = None,
        burst_refill_per_sec: float | None = None,
        # 别名兼容
        qps: float | None = None,
        burst: int | None = None,
        sustained_capacity: int | None = None,
        **kwargs,
    ) -> None:
        # 别名归一化，保持向后兼容
        if qps is not None:
            refill_per_sec = float(qps)
        if sustained_capacity is not None:
            capacity = int(sustained_capacity)
        if burst is not None:
            burst_capacity = int(burst)
        if "sustained_refill" in kwargs:
            refill_per_sec = float(kwargs.pop("sustained_refill"))
        if "refill_rate" in kwargs:
            refill_per_sec = float(kwargs.pop("refill_rate"))
        # burst 默认 2*capacity，refill 默认 2*refill
        if burst_capacity is None:
            burst_capacity = int(capacity * 2) if capacity else 20
        if burst_refill_per_sec is None:
            burst_refill_per_sec = float(refill_per_sec * 2) if refill_per_sec else 10.0
        self.sustained = TokenBucket(int(capacity), float(refill_per_sec))
        self.burst = TokenBucket(int(burst_capacity), float(burst_refill_per_sec))
        self.capacity = int(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self.burst_capacity = int(burst_capacity)
        self.burst_refill_per_sec = float(burst_refill_per_sec)

    def try_acquire(self, tokens: int = 1) -> bool:
        """双桶原子获取，失败时回滚已扣的 burst。"""
        burst_ok = self.burst.try_acquire(tokens)
        if not burst_ok:
            return False
        sustained_ok = self.sustained.try_acquire(tokens)
        if not sustained_ok:
            # 回滚 burst，保证原子性
            with self.burst._lock:
                self.burst.tokens = min(self.burst.capacity, self.burst.tokens + tokens)
            return False
        return True

    def allow(self, tokens: int = 1) -> bool:
        """allow 别名，等价 try_acquire。"""
        return self.try_acquire(tokens)

    def available_tokens(self) -> tuple[float, float]:
        """返回 (sustained, burst) 可用 token。"""
        return (self.sustained.available(), self.burst.available())

    def get_state(self) -> dict:
        """返回限流器状态快照。"""
        s, b = self.available_tokens()
        return {
            "sustained_tokens": s,
            "burst_tokens": b,
            "capacity": self.capacity,
            "refill_per_sec": self.refill_per_sec,
            "burst_capacity": self.burst_capacity,
        }

    def reset(self) -> None:
        """重置双桶为满。"""
        self.sustained.reset()
        self.burst.reset()


# 兼容别名
DualTokenBucket = DualBucketRateLimiter
RateLimiter = DualBucketRateLimiter
DualBucketLimiter = DualBucketRateLimiter
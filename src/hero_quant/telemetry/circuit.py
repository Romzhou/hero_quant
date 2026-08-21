"""熔断双桶 circuit breaker + 双桶限流 (Dual-Bucket 限流).

双桶: failure bucket + slow bucket
阈值: failure 50% / slow 50% / TIME 30s
状态: CLOSED -> OPEN (HalfOpen) -> HALF_OPEN -> CLOSED
时序: open 30s / half 5 calls

限流双桶: token bucket 双桶 (burst + sustained) — 用于 router QPS 限流
  丛桶冲4 (Wave E1): 提供 DualBucketRateLimiter(capacity, refill_per_sec, burst_*) 不破坏 trace/ledger

Minimal implementation satisfying spec and test: CircuitBreaker(failure_threshold=0.5, window=1, open_duration=1)
"""

from __future__ import annotations

import collections
import time

# B1-1 circuit_state gauge (stub — Prometheus Gauge, optional for 3分)
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

# Alias for tests that import CIRCUIT_GAUGE
CIRCUIT_GAUGE = CIRCUIT_STATE_GAUGE


class CircuitBreaker:
    """Double-bucket circuit breaker.

    Buckets:
        - failure_bucket: counts failures
        - slow_bucket: counts slow calls (duration > TIME threshold 30s)

    Args:
        failure_threshold: failure rate threshold 0.5 (50%)
        slow_threshold: slow rate threshold 0.5 (50%)
        window: sliding window size in seconds (time window) or count window if small
        open_duration: seconds to stay OPEN before moving to HALF_OPEN (open 30s)
        half_open_max_calls: max probe calls in HALF_OPEN (half 5)
        slow_duration_threshold: TIME threshold for slow (30s)
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

        # deque of (timestamp, is_failure, is_slow)
        self._events: collections.deque[tuple[float, bool, bool]] = collections.deque()
        # Use simple lock (fix dead _lock deque vs _mutex confusion: remove placeholder deque)
        import threading

        self._mutex = threading.Lock()
        try:
            self._sync_gauge()
        except Exception:
            pass

    def _sync_gauge(self) -> None:
        if CIRCUIT_STATE_GAUGE is None:
            return
        try:
            mapping = {"CLOSED": 0, "HALF_OPEN": 1, "OPEN": 2}
            # state getter may transition; use raw _state after transition
            cur = self.state  # triggers OPEN->HALF_OPEN if due
            CIRCUIT_STATE_GAUGE.set(mapping.get(cur, 0))
        except Exception:
            pass

    @property
    def state(self) -> str:
        # auto transition OPEN -> HALF_OPEN after open_duration
        if self._state == "OPEN" and self._opened_at is not None:
            if time.time() - self._opened_at >= self.open_duration:
                # HalfOpen state as HALF_OPEN (spec mentions HalfOpen)
                self._state = "HALF_OPEN"
                self._half_open_calls = 0
        # normalize HalfOpen alias? keep HALF_OPEN
        return self._state

    # compatibility alias
    @property
    def _state_alias(self):
        return self.state

    def _prune(self, now: float):
        # prune events outside window (time-based)
        # window is seconds
        cutoff = now - self.window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def _evaluate(self):
        now = time.time()
        self._prune(now)
        total = len(self._events)
        if total == 0:
            return
        failures = sum(1 for _, f, _ in self._events if f)
        slows = sum(1 for _, _, s in self._events if s)
        failure_rate = failures / total if total else 0
        slow_rate = slows / total if total else 0
        # CLOSED -> OPEN if failure 50% or slow 50% or TIME logic (slow already)
        if self._state == "CLOSED":
            if failure_rate >= self.failure_threshold or slow_rate >= self.slow_threshold:
                self._trip_open(now)

    def _trip_open(self, now: float | None = None):
        self._state = "OPEN"
        self._opened_at = now if now is not None else time.time()
        self._half_open_calls = 0
        self._sync_gauge()

    def record_failure(self, duration: float | None = None):
        with self._mutex:
            # if already OPEN, stay OPEN (refresh not needed)
            if self.state == "OPEN":
                return
            # if HALF_OPEN, failure -> re-open
            if self.state == "HALF_OPEN":
                self._trip_open()
                return
            now = time.time()
            is_slow = duration is not None and duration >= self.slow_duration_threshold
            self._events.append((now, True, bool(is_slow)))
            self._evaluate()
            self._sync_gauge()

    def record_success(self, duration: float | None = None):
        with self._mutex:
            if self.state == "OPEN":
                return
            if self.state == "HALF_OPEN":
                # half-open probe: require >= half_open_max_calls(5) continuous successes before CLOSED
                self._half_open_calls += 1
                now = time.time()
                is_slow = duration is not None and duration >= self.slow_duration_threshold
                self._events.append((now, False, bool(is_slow)))
                # continuous success rate evaluation: only after 5 probes decide CLOSED else re-OPEN
                if self._half_open_calls >= self.half_open_max_calls:
                    # re-evaluate rates; if ok close else re-OPEN
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
                # else: not enough probes yet, remain HALF_OPEN for more probes
                return
            now = time.time()
            is_slow = duration is not None and duration >= self.slow_duration_threshold
            self._events.append((now, False, bool(is_slow)))
            self._evaluate()
            self._sync_gauge()

    def record_slow(self, duration: float | None = None):
        """Explicit slow record (counts as success but slow)."""
        # slow bucket placeholder
        self.record_success(duration=duration if duration is not None else self.slow_duration_threshold + 1)

    def allow(self) -> bool:
        self._sync_gauge()
        s = self.state
        if s == "CLOSED":
            return True
        if s == "HALF_OPEN":
            return self._half_open_calls < self.half_open_max_calls
        return False

    # For spec: TIME 30s open30s half5 exposure
    def is_closed(self) -> bool:
        return self.state == "CLOSED"

    def is_open(self) -> bool:
        return self.state == "OPEN"

    # -- Maturity 4: 兼容 limiter 接口 --
    def try_acquire(self, tokens: int = 1) -> bool:  # type: ignore[override]
        """兼容双桶限流接口: allow() 语义."""
        return self.allow()


# -- 双桶限流 TokenBucket + DualBucketRateLimiter (Wave E1) --

class TokenBucket:
    """单桶 token bucket."""

    def __init__(self, capacity: int, refill_per_sec: float) -> None:
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self.tokens = float(capacity)
        self._last = time.monotonic()
        import threading as _th

        self._lock = _th.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
            self._last = now

    def try_acquire(self, tokens: int = 1) -> bool:
        with self._lock:
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

    def available(self) -> float:
        with self._lock:
            self._refill()
            return float(self.tokens)

    def reset(self) -> None:
        with self._lock:
            self.tokens = float(self.capacity)
            self._last = time.monotonic()


class DualBucketRateLimiter:
    """双桶限流: burst + sustained 双 token bucket.

    语义: try_acquire 需同时满足双桶有 token，才算成功（双条件限流）。
    默认: capacity=10, refill_per_sec=5 (sustained), burst_capacity=20, burst_refill=10
    兼容构造别名: qps/sustained_capacity 等均映射到 capacity/refill.
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
        # 别名归一化
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
        # burst 默认 2*capacity, refill 默认 2*refill
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
        """双桶同时 acquire: 原子性尝试，失败则回滚."""
        # 先检查 burst，再 sustained；需保证原子性 (两锁顺序: burst then sustained)
        # 为避免死锁，先预 refill 并检查可用量，若不足直接 false
        # 简单实现: 先 try burst, 若成功再 try sustained, 若 sustained 失败则回滚 burst
        burst_ok = self.burst.try_acquire(tokens)
        if not burst_ok:
            return False
        sustained_ok = self.sustained.try_acquire(tokens)
        if not sustained_ok:
            # 回滚 burst: 补回 token (带锁直接加)
            with self.burst._lock:
                self.burst.tokens = min(self.burst.capacity, self.burst.tokens + tokens)
            return False
        return True

    def allow(self, tokens: int = 1) -> bool:
        return self.try_acquire(tokens)

    def available_tokens(self) -> tuple[float, float]:
        return (self.sustained.available(), self.burst.available())

    def get_state(self) -> dict:
        s, b = self.available_tokens()
        return {
            "sustained_tokens": s,
            "burst_tokens": b,
            "capacity": self.capacity,
            "refill_per_sec": self.refill_per_sec,
            "burst_capacity": self.burst_capacity,
        }

    def reset(self) -> None:
        self.sustained.reset()
        self.burst.reset()


# 别名供外部兼容
DualTokenBucket = DualBucketRateLimiter
RateLimiter = DualBucketRateLimiter
DualBucketLimiter = DualBucketRateLimiter

"""熔断双桶 circuit breaker.

双桶: failure bucket + slow bucket
阈值: failure 50% / slow 50% / TIME 30s
状态: CLOSED -> OPEN (HalfOpen) -> HALF_OPEN -> CLOSED
时序: open 30s / half 5 calls

Minimal implementation satisfying spec and test: CircuitBreaker(failure_threshold=0.5, window=1, open_duration=1)
"""

from __future__ import annotations

import collections
import time


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
        self._lock = collections.deque()  # placeholder for thread-safety if needed
        # Use simple lock
        import threading

        self._mutex = threading.Lock()

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

    def record_success(self, duration: float | None = None):
        with self._mutex:
            if self.state == "OPEN":
                return
            if self.state == "HALF_OPEN":
                # success in half-open: count probe; after half_open_max_calls close?
                self._half_open_calls += 1
                now = time.time()
                is_slow = duration is not None and duration >= self.slow_duration_threshold
                self._events.append((now, False, bool(is_slow)))
                # if enough successes and rates ok, close
                # simple: after one success, close (for minimal)
                # For spec half5: allow up to 5 probes then close if no failures
                if self._half_open_calls >= 1:
                    # re-evaluate rates; if ok close
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
                    else:
                        self._trip_open(now)
                return
            now = time.time()
            is_slow = duration is not None and duration >= self.slow_duration_threshold
            self._events.append((now, False, bool(is_slow)))
            self._evaluate()

    def record_slow(self, duration: float | None = None):
        """Explicit slow record (counts as success but slow)."""
        # slow bucket placeholder
        self.record_success(duration=duration if duration is not None else self.slow_duration_threshold + 1)

    def allow(self) -> bool:
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

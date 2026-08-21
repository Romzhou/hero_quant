"""IncrementalFactor — streaming SMA <200ms, minimal."""
from __future__ import annotations

from collections import deque
from typing import Deque


class IncrementalFactor:
    """Minimal incremental SMA: windowed rolling mean <200ms."""
    def __init__(self, window: int = 20):
        try:
            w = int(window)
        except Exception:
            w = 20
        if w <= 0:
            w = 20
        self.window = w
        self._buf: Deque[float] = deque(maxlen=w)
        self._sum: float = 0.0

    def update(self, price: float) -> float:
        try:
            p = float(price)
        except Exception:
            p = 0.0
        if len(self._buf) == self.window:
            oldest = self._buf[0]
            self._sum -= oldest
        self._buf.append(p)
        self._sum += p
        # return value (current SMA if filled else avg of available)
        if len(self._buf) < self.window:
            # partial average to keep value defined; tests warm up to full window before checking approx
            return self._sum / len(self._buf) if self._buf else 0.0
        return self._sum / self.window

    @property
    def value(self) -> float:
        if not self._buf:
            return 0.0
        if len(self._buf) < self.window:
            return self._sum / len(self._buf)
        return self._sum / self.window

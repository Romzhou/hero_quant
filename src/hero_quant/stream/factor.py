"""增量因子 —— 流式 SMA（<200ms），用于实时因子流。

职责：维护窗口内的滚动均值；架构位置：stream 域，供实时数据流按价格增量更新。
设计决策：窗口大小默认 20，基于 deque 与累加和实现 O(1) 更新，满足 200ms 内响应。
"""
from __future__ import annotations

from collections import deque
from typing import Deque


class IncrementalFactor:
    """增量式 SMA：基于滑动窗口的滚动均值，窗口默认 20（可配置）。"""

    def __init__(self, window: int = 20):
        # 窗口参数归一化：非正或非法时回退到 20，保证计算有意义
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
        """输入新价格并返回当前 SMA。"""
        try:
            p = float(price)
        except Exception:
            p = 0.0
        if len(self._buf) == self.window:
            oldest = self._buf[0]
            self._sum -= oldest
        self._buf.append(p)
        self._sum += p
        # 未填满窗口时返回已有数据的均值，保证值始终有定义
        if len(self._buf) < self.window:
            # 预热阶段均值，供测试与早期信号使用
            return self._sum / len(self._buf) if self._buf else 0.0
        return self._sum / self.window

    @property
    def value(self) -> float:
        """当前 SMA 值。"""
        if not self._buf:
            return 0.0
        if len(self._buf) < self.window:
            return self._sum / len(self._buf)
        return self._sum / self.window

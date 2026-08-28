"""wall-time 指标便捷导入 —— metrics 的薄封装。

职责：重导出 observe_wall_time 等辅助，保持 from hero_quant.metrics.wall_time 导入路径稳定。
设计：采用 PEP 562 惰性代理避免初始化顺序耦合，静态别名保留以兼容直接导入。
"""

from __future__ import annotations

import importlib
from typing import Any

from . import (
    WALL_TIME_BUDGET_EXCEEDED,
    WALL_TIME_SECONDS,
    inc_wall_time_exceeded,
    observe_wall_time,
)

__all__ = [
    "WALL_TIME_SECONDS",
    "WALL_TIME_DURATION",
    "WALL_TIME_BUDGET_EXCEEDED",
    "observe_wall_time",
    "inc_wall_time_exceeded",
]

# 兼容别名：与 metrics.__init__ 保持一致（静态快照，配合 __getattr__ 实现 live 语义）
WALL_TIME_DURATION = WALL_TIME_SECONDS


def __getattr__(name: str) -> Any:
    """PEP 562 惰性代理，保持与 hero_quant.metrics 的 live 一致性。"""
    if name == "WALL_TIME_DURATION":
        import hero_quant.metrics as _m

        return _m.WALL_TIME_SECONDS
    if name in ("WALL_TIME_SECONDS", "WALL_TIME_BUDGET_EXCEEDED", "observe_wall_time", "inc_wall_time_exceeded"):
        mod = importlib.import_module("hero_quant.metrics")
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

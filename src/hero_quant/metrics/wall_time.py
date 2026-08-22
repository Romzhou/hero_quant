"""wall-time 指标便捷导入 —— metrics 的薄封装。

职责：重导出 observe_wall_time 等辅助，保持 from hero_quant.metrics.wall_time 导入路径稳定。
"""

from . import (
    WALL_TIME_BUDGET_EXCEEDED,
    WALL_TIME_SECONDS,
    inc_wall_time_exceeded,
    observe_wall_time,
)

__all__ = [
    "WALL_TIME_SECONDS",
    "WALL_TIME_BUDGET_EXCEEDED",
    "observe_wall_time",
    "inc_wall_time_exceeded",
]

# 兼容别名：与 metrics.__init__ 保持一致
WALL_TIME_DURATION = WALL_TIME_SECONDS

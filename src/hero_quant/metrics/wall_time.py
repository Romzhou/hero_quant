"""Wall-time specific helpers (re-export for metrics.wall_time path).

Thin wrapper over metrics.__init__ helpers to keep import path stable:
`from hero_quant.metrics.wall_time import observe_wall_time`
"""

from . import (
    DEDUP_OP_TOTAL,
    LEDGER_APPEND_DURATION,
    LEDGER_APPEND_TOTAL,
    REQUEST_COUNTER,
    REQUEST_DURATION,
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

# also expose alias for histogram duration
WALL_TIME_DURATION = WALL_TIME_SECONDS

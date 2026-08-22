"""governance — 治理层聚合包。

职责：对外暴露幂等去重、hash 链账本、壁钟预算与对账能力，是 Agent 运行时与持久化之间的治理边界。
架构位置：上游被 agent/loop、shadow、tools 调用，下游依赖 SQLite/PG、文件系统与 metrics。
关键设计：幂等键在编排层派生；账本以 hash chain + fsync + 0600 保证可验证与持久性；预算以 monotonic 时钟度量。
"""

from hero_quant.governance.dedup import DedupStore, derive_key
from hero_quant.governance.ledger import Ledger
from hero_quant.governance.wall_time import (
    BudgetEnforcer,
    GovernanceWallTimeBudget,
    WallTimeBudget,
    WallTimeBudgetEnforcer,
    WallTimeBudgetExceeded,
    WallTimeExceeded,
    WallTimeGovernor,
    enforce_wall_time,
    wall_time_budget,
    with_wall_time_budget,
)

__all__ = [
    "Ledger",
    "DedupStore",
    "derive_key",
    "WallTimeBudget",
    "WallTimeGovernor",
    "WallTimeExceeded",
    "WallTimeBudgetExceeded",
    "WallTimeBudgetEnforcer",
    "GovernanceWallTimeBudget",
    "BudgetEnforcer",
    "with_wall_time_budget",
    "enforce_wall_time",
    "wall_time_budget",
]

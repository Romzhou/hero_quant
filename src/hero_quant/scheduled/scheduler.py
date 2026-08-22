"""scheduler — `scheduled.service` 的兼容别名。

职责：为历史导入路径 `from hero_quant.scheduled.scheduler import ...` 提供平滑迁移。
架构位置：薄封装层，全部能力委托给 `service`。
关键设计：星号重导出 + 显式具名导出兼顾兼容与静态检查。
"""

from .service import *  # noqa: F401,F403
from .service import (
    PLAYBOOKS,
    ScheduledPlaybook,
    ScheduledService,
    Scheduler,
    get_next_trigger,
    get_playbook,
    list_playbooks,
    parse_cron,
    validate_cron,
)

__all__ = [
    "PLAYBOOKS",
    "ScheduledPlaybook",
    "ScheduledService",
    "Scheduler",
    "get_next_trigger",
    "get_playbook",
    "list_playbooks",
    "parse_cron",
    "validate_cron",
]

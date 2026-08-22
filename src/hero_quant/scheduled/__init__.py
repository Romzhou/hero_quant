"""scheduled — 定时研究任务调度。

职责：管理 5 个 playbook 的 cron 调度与时区感知分发。
架构位置：`hero_quant.scheduled`，对接 Temporal Cron，占位本地 next_trigger 计算。
关键设计：5 字段 cron + ZoneInfo 时区校验；分钟级暴力搜索（366 天 horizon）；playbook 注册表统一 `to_temporal_cron` 与 `dispatch`。
"""

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

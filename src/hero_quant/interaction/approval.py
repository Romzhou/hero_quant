"""交互侧审批 —— 基于安全审批策略的问询守卫。

职责：复用 security 层的审批策略与三段式 ask→guard，提供交互层的中断与恢复语义。
架构位置：interaction 层，对 security.approval 的薄封装，叠加问询卡片与 Command 恢复。
设计决策：ASK 模式下先触发问询中断再做守卫校验；NEVER 模式直接短路拒绝；Store 按 (tenant, thread) 隔离由调用方持有。
"""

from __future__ import annotations

from typing import Any

# 复用 security 层的审批基座
from hero_quant.security.approval import (
    ApprovalPolicy,
    ApprovalService as SecurityApprovalService,
    effectiveApprovalPolicy,
)

from .questions import AskCardInterrupt, Command


class ApprovalService(SecurityApprovalService):
    """交互层审批，扩展三段式 ask→guard 并支持问询中断语义。"""

    def request_sync_with_guard(self, tool: str, reason: str | None = None, questions: list[Any] | None = None) -> str:
        """带问询守卫的同步审批：ASK 模式下先中断问询，再执行守卫并返回决策。"""
        # 阶段一：ASK 模式且提供问询项时先触发问询中断
        if self.mode == ApprovalPolicy.ASK and questions:
            # 占位：触发 UserQuestionService 问询，此处直接抛中断由上层处理
            raise AskCardInterrupt(questions, reason="ask guard interrupt")
        # 阶段二：执行守卫校验
        outcome = self.request_sync(tool=tool, reason=reason)
        # 阶段三：通过 Command 记录决策恢复点，便于审计
        _ = Command(resume=outcome, goto="decided")
        return outcome


__all__ = ["ApprovalService", "ApprovalPolicy", "effectiveApprovalPolicy", "AskCardInterrupt", "Command"]

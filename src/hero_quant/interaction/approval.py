"""交互侧审批 —— 基于安全审批策略的问询守卫。

职责：复用 security 层的审批策略与三段式 ask→guard，提供交互层的中断与恢复语义。
架构位置：interaction 层，对 security.approval 的薄封装，叠加问询卡片与 Command 恢复。
设计决策：先执行守卫校验（策略/denylist）再触发问询中断；Command 携带 outcome 至 decided 节点并审计。
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

    def request_sync_with_guard(self, tool: str, reason: str | None = None, questions: list[Any] | None = None) -> Any:
        """带问询守卫的同步审批：先守卫校验，再按需中断问询，最终通过 Command 恢复至 decided 节点并审计。"""
        # 延迟导入审计，避免循环依赖
        try:
            from hero_quant.security.approval import _audit
        except Exception:  # pragma: no cover
            def _audit(event, **fields):  # type: ignore
                pass

        # 阶段零：守卫前置 — denylist 与 NEVER 策略在中断前执行，fail-closed
        disallowed_set = {"disallowed_tool", "forbidden_tool", "dangerous_tool", "rm -rf", "delete_all", "drop_table"}
        is_disallowed = (
            tool in disallowed_set
            or tool.startswith("forbidden")
            or "disallowed" in tool
            or "dangerous" in tool
        )
        if is_disallowed:
            outcome: Any = "rejected"
            try:
                _audit("decided", tool=tool, outcome=outcome, reason=reason, guard="disallowed", goto="decided")
            except Exception:
                pass
            cmd = Command(resume=outcome, goto="decided")
            return cmd

        # NEVER 策略直接短路拒绝，不触达问询中断
        if self.mode == ApprovalPolicy.NEVER:
            outcome = self.request_sync(tool=tool, reason=reason)
            # request_sync 已审计 rejected，但补充 goto/resume 审计以满足 langgraph 恢复点可追溯
            try:
                _audit("decided", tool=tool, outcome=outcome, goto="decided", resume=outcome)
            except Exception:
                pass
            cmd = Command(resume=outcome, goto="decided")
            return cmd

        # 阶段一：ASK 模式且提供问询项时，在守卫通过后触发问询中断
        if self.mode == ApprovalPolicy.ASK and questions:
            # 守卫已在上方执行，此处仅在通过后中断
            raise AskCardInterrupt(questions, reason="ask guard interrupt")

        # 阶段二：执行守卫校验（复用 security 基座）
        outcome = self.request_sync(tool=tool, reason=reason)
        # 阶段三：通过 Command 记录决策恢复点并审计，便于上层恢复与审计追溯
        cmd = Command(resume=outcome, goto="decided")
        try:
            _audit("decided", tool=tool, outcome=outcome, goto=cmd.goto, resume=cmd.resume)
        except Exception:
            pass
        return cmd


__all__ = ["ApprovalService", "ApprovalPolicy", "effectiveApprovalPolicy", "AskCardInterrupt", "Command"]

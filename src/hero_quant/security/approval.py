"""高危操作审批 — 人审流程与 fail-closed 策略折叠。

职责：对工具调用等高危操作提供 ask/never/auto 三档审批语义。
安全设计：倒序折叠取最后显式策略，未显式则默认 ask；never 模式在服务层
直接短路为 rejected，不触达外部审批；审批事件以结构化日志落审计占位。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class ApprovalPolicy:
    """审批策略常量（ask/never/auto），支持字符串比较与枚举式使用。"""

    ASK = "ask"
    NEVER = "never"
    AUTO = "auto"

    def __init__(self, value: str):
        v = value.strip().lower() if isinstance(value, str) else str(value).lower()
        if v not in ("ask", "never", "auto"):
            raise ValueError(f"unknown approval policy: {value}")
        self.value = v

    def __str__(self):
        return self.value

    def __eq__(self, other):
        if isinstance(other, ApprovalPolicy):
            return self.value == other.value
        if isinstance(other, str):
            return self.value == other.lower()
        return False


def effectiveApprovalPolicy(events: list[dict[str, Any]] | None) -> str:
    """倒序折叠取最后显式策略，未显式则默认为 ask。"""
    if not events:
        return ApprovalPolicy.ASK
    for ev in reversed(events):
        # 兼容多键名（policy / approval_policy / mode 等）
        for k in ("policy", "approval_policy", "effective_policy", "mode"):
            if k in ev and ev[k] in ("ask", "never", "auto", ApprovalPolicy.ASK, ApprovalPolicy.NEVER, ApprovalPolicy.AUTO):
                val = ev[k]
                if isinstance(val, ApprovalPolicy):
                    return val.value
                return str(val).lower()
        # 兼容历史事件类型
        if ev.get("type") in ("approval/asked", "approval/decided") and "policy" in ev:
            return str(ev["policy"]).lower()
    return ApprovalPolicy.ASK


def _audit(event: str, **fields):
    """内审计占位——以结构化日志记录审批轨迹，后续可对接 ledger/otel。"""
    try:
        logger.info("approval.%s", event, extra=fields)
    except Exception:
        pass


def requires_approval(policy: str) -> bool:
    """模块级 helper：判断策略是否需要人审（仅 ask 需审批）。"""
    p = policy.strip().lower() if isinstance(policy, str) else str(policy).lower()
    return p == ApprovalPolicy.ASK


@dataclass
class ApprovalService:
    """审批服务：ask 需人审、never 直接拒绝，保障高危操作 fail-closed。"""

    mode: str = "ask"

    def __post_init__(self):
        self.mode = self.mode.strip().lower() if isinstance(self.mode, str) else "ask"
        if self.mode not in ("ask", "never", "auto"):
            self.mode = "ask"

    def requires_approval(self, tool: str | None = None) -> bool:  # noqa: ARG002
        """实例 helper：当前模式是否需要人审（ask→True，其余 False）。"""
        return self.mode == ApprovalPolicy.ASK

    def request_sync(self, tool: str, reason: str | None = None, **kwargs: Any) -> Any:
        """同步审批：never 短路 rejected，ask 返回 pending 阻塞，auto 直接 approved。"""
        _audit("asked", tool=tool, reason=reason, mode=self.mode)
        if self.mode == ApprovalPolicy.NEVER:
            _audit("decided", tool=tool, outcome="rejected", reason=reason)
            return "rejected"
        if self.mode == ApprovalPolicy.ASK:
            # P2 blocking: 返回 pending/need_approval 由调用方处理阻塞与超时（300s）
            _audit("asked_pending", tool=tool, reason=reason, timeout=300)
            return {
                "status": "pending",
                "need_approval": True,
                "timeout": 300,
                "tool": tool,
                "reason": reason,
                "mode": self.mode,
            }
        # auto 直通
        _audit("decided", tool=tool, outcome="approved", reason=reason)
        return "approved"

    async def request(self, tool: str, reason: str | None = None, **kwargs: Any) -> Any:
        """异步审批入口，当前委托同步实现。"""
        return self.request_sync(tool=tool, reason=reason, **kwargs)

    def effective_policy(self, events: list[dict[str, Any]] | None = None) -> str:
        """结合历史事件折叠与当前模式，计算最终生效策略。"""
        if not events:
            return self.mode
        folded = effectiveApprovalPolicy(events)
        # fail-closed: service mode is ceiling — return most restrictive of mode vs folded
        # never(0) > ask(1) > auto(2)  — lower value = more restrictive
        order = {ApprovalPolicy.NEVER: 0, ApprovalPolicy.ASK: 1, ApprovalPolicy.AUTO: 2}
        mode_rank = order.get(self.mode, 1)
        folded_rank = order.get(folded, 1)
        # if folded is more permissive than service mode, clamp to service mode
        if folded_rank > mode_rank:
            return self.mode
        # also explicitly forbid ask->auto escalation via untrusted events
        if self.mode == ApprovalPolicy.ASK and folded == ApprovalPolicy.AUTO:
            return ApprovalPolicy.ASK
        return folded

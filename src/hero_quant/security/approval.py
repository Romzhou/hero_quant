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

    def __repr__(self):
        return f"ApprovalPolicy({self.value!r})"

    def __hash__(self):
        # 与 __str__/__eq__ 一致：按策略值哈希，保证可入 set/dict
        return hash(self.value)

    def __eq__(self, other):
        if isinstance(other, ApprovalPolicy):
            return self.value == other.value
        # 中文：不与 plain str 相等——跨类型相等会破坏 hash/eq 契约
        # （case-insensitive 相等但 hash 取归一化值，dict/set 混用时静默 miss）。
        # 调用方改用 str(policy) == s 或 policy.value == s.strip().lower()。
        return NotImplemented


def effectiveApprovalPolicy(events: list[dict[str, Any]] | None) -> str:
    """倒序折叠取最后显式策略，未显式则默认为 ask。"""
    if not events:
        return ApprovalPolicy.ASK
    for ev in reversed(events):
        if not isinstance(ev, dict):
            continue
        # 兼容多键名（policy / approval_policy / mode 等），支持 heterogeneous str|dict
        for k in ("policy", "approval_policy", "effective_policy", "mode"):
            if k not in ev:
                continue
            val = ev[k]
            # heterogeneous: val may be str or dict {value/policy/mode}
            if isinstance(val, dict):
                # extract inner string policy from dict
                inner = None
                for ik in ("policy", "value", "mode", "approval_policy", "effective_policy"):
                    if ik in val:
                        inner = val[ik]
                        break
                if inner is None:
                    continue
                val = inner
            if isinstance(val, ApprovalPolicy):
                return val.value
            if isinstance(val, str):
                lv = val.strip().lower()
                if lv in ("ask", "never", "auto"):
                    return lv
                continue
            # fallback: coerce via str
            try:
                lv = str(val).strip().lower()
                if lv in ("ask", "never", "auto"):
                    return lv
            except (ValueError, TypeError, AttributeError):
                continue
    return ApprovalPolicy.ASK


def _audit(event: str, **fields):
    """内审计占位——以结构化日志记录审批轨迹，后续可对接 ledger/otel。"""
    try:
        logger.info("approval.%s", event, extra=fields)
    except Exception as exc:
        # 中文：审计失败不得静默吞掉（fail-open 日志即审计缺口与成功不可区分）。
        # 回退 warning 可见；仍失败则 RuntimeWarning 告警，永不静默。
        try:
            logger.warning("approval.audit_failed event=%s error=%r", event, exc)
        except Exception:
            try:
                import warnings

                warnings.warn(f"approval audit failed: event={event} error={exc!r}", RuntimeWarning, stacklevel=2)
            except Exception:
                pass


def requires_approval(policy: object) -> bool:
    """模块级 helper：判断策略是否需要人审（fail-closed：仅 never/auto 放行）。

    未知/None/非法策略一律视为需要审批，避免静默绕过人审。
    """
    if isinstance(policy, ApprovalPolicy):
        return policy.value == ApprovalPolicy.ASK
    if isinstance(policy, str):
        p = policy.strip().lower()
        if p in (ApprovalPolicy.NEVER, ApprovalPolicy.AUTO):
            return False
        return True
    return True


class _Decision(dict):
    """审批决议：dict 形态承载 status，同时兼容旧 `== 'rejected'` 字符串比较。"""

    def __init__(self, status: str, **fields: Any):
        super().__init__(status=status, **fields)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            # 中文：两侧同时归一化（存储 status 未必小写），兑现大小写不敏感比较承诺
            return str(self.get("status", "")).lower() == other.lower()
        return super().__eq__(other)

    def __ne__(self, other: object) -> bool:
        eq = self.__eq__(other)
        if eq is NotImplemented:
            return eq
        return not eq

    # 中文：_Decision 为可变 dict 子类，保持显式不可哈希（dict.__hash__ 为 None，
    # 旧实现 dict.__hash__(self) 必抛 TypeError 且语义含混；显式 None 语义清晰）。
    __hash__ = None  # type: ignore[assignment]


@dataclass
class ApprovalService:
    """审批服务：ask 需人审、never 直接拒绝，保障高危操作 fail-closed。"""

    mode: str = "ask"

    def __post_init__(self):
        # 非 str mode（如 ApprovalPolicy 实例）按其 value 归一化，不静默丢成 ask
        if isinstance(self.mode, ApprovalPolicy):
            raw = self.mode.value
        elif isinstance(self.mode, str):
            raw = self.mode.strip().lower()
        else:
            try:
                raw = str(self.mode).strip().lower()
            except (ValueError, TypeError, AttributeError):
                raw = "ask"
        self.mode = raw if raw in ("ask", "never", "auto") else "ask"

    def requires_approval(self, tool: str | None = None) -> bool:  # noqa: ARG002
        """实例 helper：当前模式是否需要人审（ask→True，其余 False）。"""
        return self.mode == ApprovalPolicy.ASK

    def request_sync(self, tool: str, reason: str | None = None, **kwargs: Any) -> _Decision:
        """同步审批：统一返回含 status 的决议（never→rejected，ask→pending，auto→approved）。"""
        _audit("asked", tool=tool, reason=reason, mode=self.mode)
        if self.mode == ApprovalPolicy.NEVER:
            _audit("decided", tool=tool, outcome="rejected", reason=reason)
            return _Decision("rejected", tool=tool, reason=reason, mode=self.mode)
        if self.mode == ApprovalPolicy.ASK:
            # P2 blocking: 返回 pending/need_approval 由调用方处理阻塞与超时（300s）
            _audit("asked_pending", tool=tool, reason=reason, timeout=300)
            return _Decision(
                "pending",
                need_approval=True,
                timeout=300,
                tool=tool,
                reason=reason,
                mode=self.mode,
            )
        # auto 直通
        _audit("decided", tool=tool, outcome="approved", reason=reason)
        return _Decision("approved", tool=tool, reason=reason, mode=self.mode)

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
        return folded

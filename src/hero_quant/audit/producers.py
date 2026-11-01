"""audit.producers — 业务侧审计事件生产者（PR-241），**只由主控接线**。

三个固定动作：tool.call / backtest.run / session.run。约束：

- 走 AuditChain（入链），不是普通 append：这三类事件必须能被 /v1/audit/verify 覆盖，
  否则「链」会因大量未入链行而失去意义。
- 失败返回 None 并打 warning，**绝不抛**：审计是旁路，不能改变工具/回测/会话的响应。
- 日志只带 action / tenant / entry_id / error_code，绝不打 detail 原文或 prompt/结果。
- 本模块不 import server.py / loop.py，接线由主控在调用点完成。
"""

from __future__ import annotations

from typing import Any, Mapping

import structlog

from hero_quant.audit.chain import AuditChain
from hero_quant.audit.store import AuditError, get_audit_store

logger = structlog.get_logger("audit.producers")

ACTION_TOOL_CALL = "tool.call"
ACTION_BACKTEST_RUN = "backtest.run"
ACTION_SESSION_RUN = "session.run"


def _clean_detail(detail: Mapping[str, Any] | None) -> dict[str, Any]:
    """去掉 None 值（不写「未知」占位），其余交给 store 的敏感键黑名单把关。"""
    if not detail:
        return {}
    return {str(key): value for key, value in detail.items() if value is not None}


def _record(
    action: str,
    tenant_id: str | None,
    *,
    resource: str | None,
    run_id: str | None,
    user_id: str | None,
    detail: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """内部统一入口：租户缺失/存储不可用/入参非法都收敛成 None + warning。"""
    tenant = tenant_id.strip() if isinstance(tenant_id, str) else ""
    if not tenant:
        logger.warning(
            "audit.producer_skipped",
            error_code="audit_tenant_missing",
            action=action,
        )
        return None
    try:
        chain = AuditChain(get_audit_store())
        return chain.append(
            tenant,
            action,
            resource=resource,
            run_id=run_id,
            user_id=user_id,
            detail=_clean_detail(detail),
        )
    except AuditError as exc:
        logger.warning(
            "audit.producer_failed",
            error_code=getattr(exc, "error", "audit_error"),
            action=action,
            tenant=tenant,
            reason=getattr(exc, "reason", None),
            error_type=type(exc).__name__,
        )
        return None
    except (ValueError, TypeError) as exc:
        logger.warning(
            "audit.producer_failed",
            error_code="audit_invalid",
            action=action,
            tenant=tenant,
            error_type=type(exc).__name__,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — 审计是旁路：任何驱动/池异常都不得冒到业务响应
        logger.warning(
            "audit.producer_failed",
            error_code="audit_producer_failed",
            action=action,
            tenant=tenant,
            error_type=type(exc).__name__,
        )
        return None


def record_tool_call_audit(
    tenant_id: str | None,
    *,
    tool: str,
    run_id: str | None = None,
    user_id: str | None = None,
    ok: bool | None = None,
    duration_ms: int | None = None,
    error_code: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """记录一次工具调用（action=tool.call）；成功返回落库行，失败返回 None。"""
    payload = _clean_detail(detail)
    if ok is not None:
        payload["ok"] = bool(ok)
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if error_code is not None:
        payload["error_code"] = error_code
    return _record(
        ACTION_TOOL_CALL,
        tenant_id,
        resource=tool,
        run_id=run_id,
        user_id=user_id,
        detail=payload,
    )


def record_backtest_audit(
    tenant_id: str | None,
    *,
    run_id: str | None = None,
    user_id: str | None = None,
    strategy: str | None = None,
    ok: bool | None = None,
    error_code: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """记录一次回测运行（action=backtest.run）；成功返回落库行，失败返回 None。"""
    payload = _clean_detail(detail)
    if ok is not None:
        payload["ok"] = bool(ok)
    if error_code is not None:
        payload["error_code"] = error_code
    return _record(
        ACTION_BACKTEST_RUN,
        tenant_id,
        resource=strategy,
        run_id=run_id,
        user_id=user_id,
        detail=payload,
    )


def record_session_audit(
    tenant_id: str | None,
    *,
    run_id: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    ok: bool | None = None,
    error_code: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """记录一次会话运行（action=session.run）；成功返回落库行，失败返回 None。"""
    payload = _clean_detail(detail)
    if ok is not None:
        payload["ok"] = bool(ok)
    if error_code is not None:
        payload["error_code"] = error_code
    return _record(
        ACTION_SESSION_RUN,
        tenant_id,
        resource=session_id,
        run_id=run_id,
        user_id=user_id,
        detail=payload,
    )


__all__ = [
    "ACTION_BACKTEST_RUN",
    "ACTION_SESSION_RUN",
    "ACTION_TOOL_CALL",
    "record_backtest_audit",
    "record_session_audit",
    "record_tool_call_audit",
]

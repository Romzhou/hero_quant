"""admin.api — 管理后台只读总览 + killswitch 开关（PR-215）。

端点（相对路径；主控用 /v1/admin 与 /admin 双注册）：

- `GET /overview`       各状态 run 计数 / 审计条数 / 最老 queued 等待秒数
- `GET /tenants`        租户清单（runs 数 + 最后活动时间），跨租户
- `GET /runs`           只读 run 看板（status/limit，租户内）
- `GET /quotas`         当前租户配额用量（速率窗口 / 日 token 预算）
- `GET /audit/summary`  审计条数 + 最近 N 条（无 detail 原文）+ 链校验摘要
- `GET /toggles` / `POST /toggles`  killswitch 列表与启停

架构位置：admin 包自身的 FastAPI 接缝；server.py 只 include_router，不写业务。

关键设计（fail-closed，绝不假成功）：
- 全部端点要求 admin：复用 `auth.deps.require_role("admin")`，并把
  `InsufficientRole` 显式映射成 403——不依赖 app 是否装了全局异常处理器；
- 租户只从 contextvars 取（`require_tenant()`）；缺失即 503，绝不回退默认租户；
- 数据源不可用 → 对应字段 `null` + `degraded:true` + 稳定 `reason`，**不填 0**；
- 跨租户视图需要 DB 角色 SUPERUSER/BYPASSRLS；没有权限时 overview 退回租户内并显式
  标注 `scope:"tenant"`，tenants 直接返回 null + reason（不让 RLS 过滤冒充空数据）；
- 异常原文不回显：对外只给稳定 reason，细节进 structlog。
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone
from typing import Any

import structlog
import structlog.contextvars
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from hero_quant.admin.store import (
    AdminScopeUnavailable,
    AdminStore,
    AdminStoreUnavailable,
)
from hero_quant.audit.chain import AuditChain
from hero_quant.audit.store import (
    AuditStoreUnavailable,
    AuditValidationError as AuditStoreValidationError,
    get_audit_store,
)
from hero_quant.auth.context import current_principal, require_tenant
from hero_quant.auth.deps import require_role
from hero_quant.auth.errors import InsufficientRole, MissingTenant
from hero_quant.auth.quota import default_quota
from hero_quant.infra.redis import get_redis
from hero_quant.killswitch.switch import (
    KillSwitch,
    KillSwitchError,
    KillSwitchUnavailable,
    KillSwitchValidationError,
)
from hero_quant.runs.store import (
    RUN_STATUSES,
    RunQueue,
    RunsUnavailable,
    RunsValidationError,
)

logger = structlog.get_logger("admin")

router = APIRouter()

DEFAULT_RUNS_LIMIT = 50
MAX_RUNS_LIMIT = 100
DEFAULT_TENANTS_LIMIT = 50
MAX_TENANTS_LIMIT = 100
DEFAULT_AUDIT_LIMIT = 20
MAX_AUDIT_LIMIT = 100
TOGGLE_ACTIONS = frozenset({"enable", "disable"})
TOGGLE_AUDIT_ACTION = "admin.toggle"

# 审计摘要的公开列：detail 可能含业务引用，一律不出 admin 接口。
_PUBLIC_AUDIT_COLUMNS = (
    "id",
    "ts",
    "user_id",
    "action",
    "resource",
    "run_id",
    "model",
    "prompt_hash",
    "result_hash",
)

#: 已知 run 状态（取自 runs.store，避免两处漂移）；缺失即 0（查询已覆盖全量）。
_RUN_STATUSES = tuple(sorted(RUN_STATUSES))


# ---------------------------------------------------------------------------
# 身份 / 租户闸
# ---------------------------------------------------------------------------
_admin_role_dependency = require_role("admin")


async def require_admin() -> None:
    """admin 闸：已绑定主体必须是 admin；未绑定主体放行到 503（绝不匿名放行）。

    中文：判定复用 `auth.deps.require_role("admin")`，只把 `InsufficientRole` 显式
    映射成 403——本路由可能挂在没装 `install_auth_error_handlers` 的 app（测试/嵌入），
    403 不能依赖全局处理器才成立。无主体时统一由 `_tenant()` 出 503。
    """
    principal = current_principal()
    if principal is None:
        return
    try:
        await _admin_role_dependency(principal)
    except InsufficientRole as exc:
        raise HTTPException(
            status_code=403, detail={"reason": "admin_role_required"}
        ) from exc


def _tenant() -> str:
    """当前请求租户；未认证/未绑定即 503，绝不回退 default。"""
    try:
        return require_tenant()
    except MissingTenant as exc:
        raise HTTPException(
            status_code=503, detail={"reason": "auth_required_for_admin"}
        ) from exc


def _trace_id() -> str | None:
    value = structlog.contextvars.get_contextvars().get("trace_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


# ---------------------------------------------------------------------------
# 降级封装
# ---------------------------------------------------------------------------
def _ok() -> dict[str, Any]:
    return {"degraded": False, "reason": None, "reasons": []}


def _degrade(*reasons: str) -> dict[str, Any]:
    """统一的降级标记：reason 取首个，reasons 保留全部（去重、保序）。"""
    unique: list[str] = []
    for reason in reasons:
        if reason and reason not in unique:
            unique.append(reason)
    return {"degraded": True, "reason": unique[0] if unique else None, "reasons": unique}


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def _internal_error(op: str, exc: BaseException) -> HTTPException:
    """未预期异常：只回通用 code，原文/类型进日志。"""
    logger.error(
        "admin.endpoint_failed",
        error_code="admin_error",
        op=op,
        trace_id=_trace_id(),
        error_type=type(exc).__name__,
        exc_info=True,
    )
    return HTTPException(status_code=500, detail={"reason": "admin_error"})


def _public_audit_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in _PUBLIC_AUDIT_COLUMNS}


# ---------------------------------------------------------------------------
# 依赖工厂（测试可 monkeypatch；路由内不直接 new）
# ---------------------------------------------------------------------------
def get_admin_store() -> AdminStore:
    return AdminStore.from_settings()


def get_run_queue() -> RunQueue:
    return RunQueue.from_dsn()


def get_kill_switch() -> KillSwitch:
    return KillSwitch()


# ---------------------------------------------------------------------------
# GET /overview
# ---------------------------------------------------------------------------
@router.get("/overview")
async def overview(_admin: None = Depends(require_admin)) -> dict[str, Any]:
    """平台（或当前租户）总览。

    中文：优先平台视图；DB 角色没有跨租户权限时退回当前租户，并通过
    `scope:"tenant"` + `cross_tenant_not_permitted` 显式告知范围被收窄——
    绝不把 RLS 过滤后的空集当成「平台没有数据」。
    """
    tenant = _tenant()
    reasons: list[str] = []
    try:
        store = get_admin_store()
        try:
            data = await asyncio.to_thread(store.overview, None)
            scope = "platform"
        except AdminScopeUnavailable as exc:
            data = await asyncio.to_thread(store.overview, tenant)
            scope = "tenant"
            reasons.append(exc.reason)
    except AdminStoreUnavailable as exc:
        logger.warning(
            "admin.overview_unavailable",
            error_code="pg_unavailable",
            trace_id=_trace_id(),
            reason=exc.reason,
        )
        return {
            "scope": "platform",
            "tenant_id": tenant,
            "runs": None,
            "audit": None,
            "queue": None,
            **_degrade("pg_unavailable"),
        }
    except Exception as exc:  # noqa: BLE001 — 未预期异常统一脱敏 500
        raise _internal_error("overview", exc) from exc

    by_status = {status: int(data["runs"]["by_status"].get(status, 0)) for status in _RUN_STATUSES}
    envelope = _degrade(*reasons) if reasons else _ok()
    return {
        "scope": scope,
        "tenant_id": tenant,
        "runs": {"total": int(data["runs"]["total"]), "by_status": by_status},
        "audit": data["audit"],
        "queue": data["queue"],
        **envelope,
    }


# ---------------------------------------------------------------------------
# GET /tenants
# ---------------------------------------------------------------------------
@router.get("/tenants")
async def tenants(
    limit: int = Query(DEFAULT_TENANTS_LIMIT, description=f"默认 {DEFAULT_TENANTS_LIMIT}，上限 {MAX_TENANTS_LIMIT}"),
    _admin: None = Depends(require_admin),
) -> dict[str, Any]:
    """跨租户清单（runs 数 + 最后活动时间）；无权限/无 PG 时显式降级。"""
    _tenant()
    applied = _clamp(limit, 1, MAX_TENANTS_LIMIT)
    try:
        rows = await asyncio.to_thread(get_admin_store().tenants, limit=applied)
    except AdminScopeUnavailable as exc:
        logger.warning(
            "admin.tenants_scope_denied",
            error_code="cross_tenant_not_permitted",
            trace_id=_trace_id(),
            reason=exc.reason,
        )
        return {"tenants": None, "count": None, "limit": applied, **_degrade(exc.reason)}
    except AdminStoreUnavailable as exc:
        logger.warning(
            "admin.tenants_unavailable",
            error_code="pg_unavailable",
            trace_id=_trace_id(),
            reason=exc.reason,
        )
        return {"tenants": None, "count": None, "limit": applied, **_degrade("pg_unavailable")}
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("tenants", exc) from exc
    return {"tenants": rows, "count": len(rows), "limit": applied, **_ok()}


# ---------------------------------------------------------------------------
# GET /runs
# ---------------------------------------------------------------------------
@router.get("/runs")
async def list_runs(
    status: str | None = Query(None, description="queued/running/succeeded/failed/cancelled/cancelling/dead"),
    limit: int = Query(DEFAULT_RUNS_LIMIT, description=f"默认 {DEFAULT_RUNS_LIMIT}，上限 {MAX_RUNS_LIMIT}"),
    _admin: None = Depends(require_admin),
) -> dict[str, Any]:
    """只读 run 看板：直接走 RunQueue.list（租户内，不 HTTP 自调）。"""
    tenant = _tenant()
    applied = _clamp(limit, 1, MAX_RUNS_LIMIT)

    def _load() -> list[dict[str, Any]]:
        return get_run_queue().list(tenant, status=status, limit=applied)

    try:
        rows = await asyncio.to_thread(_load)
    except RunsValidationError as exc:
        raise HTTPException(
            status_code=400, detail={"reason": "runs_invalid", "detail": exc.reason}
        ) from exc
    except RunsUnavailable as exc:
        logger.warning(
            "admin.runs_unavailable",
            error_code="runs_unavailable",
            trace_id=_trace_id(),
            reason=exc.reason,
        )
        return {
            "runs": None,
            "count": None,
            "limit": applied,
            "status": status,
            "tenant_id": tenant,
            **_degrade("runs_unavailable"),
        }
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("runs", exc) from exc
    return {
        "runs": rows,
        "count": len(rows),
        "limit": applied,
        "status": status,
        "tenant_id": tenant,
        **_ok(),
    }


# ---------------------------------------------------------------------------
# GET /quotas
# ---------------------------------------------------------------------------
async def _redis_client() -> Any:
    try:
        return await get_redis()
    except Exception as exc:  # noqa: BLE001 — Redis 建连失败按不可用处理
        logger.warning(
            "admin.quota_redis_failed",
            error_code="redis_unavailable",
            trace_id=_trace_id(),
            error_type=type(exc).__name__,
        )
        return None


@router.get("/quotas")
async def quotas(_admin: None = Depends(require_admin)) -> dict[str, Any]:
    """当前租户配额：速率策略 + 当日 token 用量。

    中文：`TenantQuotaLimiter.usage()` 读失败会按 0 返回（其注释口径是「仅观测」），
    所以先用 `ping()` 探活，探活失败一律 null + reason，绝不把不可用写成 0。
    速率滑动窗口计数没有公开读接口 → `rate_window_used` 恒为 null 并带原因。
    """
    tenant = _tenant()
    reasons: list[str] = []
    try:
        limiter = default_quota()
        policy = limiter.policy
    except Exception as exc:  # noqa: BLE001 — 配置读取失败等同不可用
        logger.warning(
            "admin.quota_config_failed",
            error_code="quota_config_unavailable",
            trace_id=_trace_id(),
            error_type=type(exc).__name__,
        )
        return {
            "tenant_id": tenant,
            "policy": None,
            "tokens_used": None,
            "tokens_remaining": None,
            "usage_ratio": None,
            "unlimited": None,
            "rate_window_used": None,
            "rate_window_reason": "quota_config_unavailable",
            **_degrade("quota_config_unavailable"),
        }

    budget = int(policy.daily_token_budget)
    used: int | None = None
    client = await _redis_client()
    if client is None:
        reasons.append("redis_unavailable")
    else:
        try:
            pong = client.ping()
            if inspect.isawaitable(pong):
                await pong
            used = int(await limiter.usage(tenant))
        except Exception as exc:  # noqa: BLE001 — 探测/读取失败都按不可用处理
            logger.warning(
                "admin.quota_redis_failed",
                error_code="redis_unavailable",
                trace_id=_trace_id(),
                error_type=type(exc).__name__,
            )
            reasons.append("redis_unavailable")
            used = None

    unlimited = budget <= 0
    remaining: int | None = None
    ratio: float | None = None
    if used is not None and not unlimited:
        remaining = max(0, budget - used)
        ratio = used / budget
    envelope = _degrade(*reasons) if reasons else _ok()
    return {
        "tenant_id": tenant,
        "policy": {
            "requests_per_minute": int(policy.requests_per_minute),
            "window_seconds": int(policy.window_seconds),
            "daily_token_budget": budget,
        },
        "tokens_used": used,
        "tokens_remaining": remaining,
        "usage_ratio": ratio,
        "unlimited": unlimited,
        "rate_window_used": None,
        "rate_window_reason": "rate_window_not_exposed",
        **envelope,
    }


# ---------------------------------------------------------------------------
# GET /audit/summary
# ---------------------------------------------------------------------------
@router.get("/audit/summary")
async def audit_summary(
    limit: int = Query(DEFAULT_AUDIT_LIMIT, description=f"最近 N 条，默认 {DEFAULT_AUDIT_LIMIT}，上限 {MAX_AUDIT_LIMIT}"),
    _admin: None = Depends(require_admin),
) -> dict[str, Any]:
    """审计条数 + 最近 N 条（无 detail）+ 链校验摘要（字段与 AuditChain.verify 一致）。"""
    tenant = _tenant()
    applied = _clamp(limit, 1, MAX_AUDIT_LIMIT)

    def _load() -> tuple[int, list[dict[str, Any]], dict[str, Any]]:
        store = get_audit_store()
        total = get_admin_store().audit_total(tenant)
        recent = store.query(tenant, limit=applied)
        report = AuditChain(store).verify(tenant)
        return total, recent, report

    try:
        total, recent, report = await asyncio.to_thread(_load)
    except (AuditStoreUnavailable, AdminStoreUnavailable) as exc:
        logger.warning(
            "admin.audit_unavailable",
            error_code="audit_store_unavailable",
            trace_id=_trace_id(),
            reason=getattr(exc, "reason", None),
        )
        return {
            "tenant_id": tenant,
            "entries": None,
            "recent": None,
            "recent_limit": applied,
            "chain": None,
            **_degrade("audit_store_unavailable"),
        }
    except AuditStoreValidationError as exc:
        raise HTTPException(
            status_code=400, detail={"reason": "audit_invalid", "detail": exc.reason}
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("audit_summary", exc) from exc

    return {
        "tenant_id": tenant,
        "entries": total,
        "recent": [_public_audit_row(row) for row in recent],
        "recent_limit": applied,
        "chain": report,
        **_ok(),
    }


# ---------------------------------------------------------------------------
# GET /toggles
# ---------------------------------------------------------------------------
@router.get("/toggles")
async def list_toggles(
    scope: str | None = Query(None, description="tenant/model/tool；缺省列出全部"),
    _admin: None = Depends(require_admin),
) -> dict[str, Any]:
    """列出当前活动开关（运维总览）。"""
    _tenant()
    try:
        active = await get_kill_switch().list_active(scope)
    except KillSwitchValidationError as exc:
        raise HTTPException(
            status_code=400, detail={"reason": "killswitch_invalid", "detail": exc.reason}
        ) from exc
    except KillSwitchUnavailable as exc:
        logger.warning(
            "admin.toggles_unavailable",
            error_code="killswitch_unavailable",
            trace_id=_trace_id(),
            reason=exc.reason,
        )
        return {"toggles": None, "count": None, **_degrade("killswitch_unavailable")}
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("toggles_list", exc) from exc
    return {"toggles": active, "count": len(active), **_ok()}


# ---------------------------------------------------------------------------
# POST /toggles
# ---------------------------------------------------------------------------
class ToggleRequest(BaseModel):
    """killswitch 启停入参；action 非法由端点显式判 400（不用 422 代替）。"""

    scope: str
    target: str
    action: str
    reason: str


async def _record_toggle(
    *, tenant: str, actor: str, scope: str, target: str, action: str, reason: str
) -> bool:
    """写 admin.toggle 审计（入链）；失败返回 False，绝不改开关结果。"""

    def _append() -> None:
        AuditChain(get_audit_store()).append(
            tenant,
            TOGGLE_AUDIT_ACTION,
            user_id=actor,
            resource=f"{scope}:{target}",
            run_id=_trace_id(),
            detail={"scope": scope, "target": target, "action": action, "reason": reason},
            ts=datetime.now(timezone.utc),
        )

    try:
        await asyncio.to_thread(_append)
        return True
    except Exception as exc:  # noqa: BLE001 — 审计是旁路：失败只在响应里显式标注
        logger.warning(
            "admin.toggle_audit_failed",
            error_code="audit_append_failed",
            trace_id=_trace_id(),
            scope=scope,
            action=action,
            error_type=type(exc).__name__,
        )
        return False


@router.post("/toggles")
async def set_toggle(
    body: ToggleRequest,
    _admin: None = Depends(require_admin),
) -> dict[str, Any]:
    """开启/关闭 killswitch，并写一条 admin.toggle 审计。"""
    tenant = _tenant()
    principal = current_principal()
    actor = getattr(principal, "subject", None)
    if not actor:
        raise HTTPException(status_code=503, detail={"reason": "auth_required_for_admin"})
    if body.action not in TOGGLE_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail={"reason": "toggle_action_invalid", "detail": "action_must_be_enable_or_disable"},
        )

    # tenant 作用域本身就是按租户停用，不再叠加租户前缀；model/tool 默认只作用于本租户。
    kill_tenant = None if body.scope == "tenant" else tenant
    switch = get_kill_switch()
    try:
        if body.action == "enable":
            state = await switch.enable(
                body.scope,
                body.target,
                body.reason,
                actor=actor,
                tenant_id=kill_tenant,
            )
            changed = True
        else:
            state = None
            changed = await switch.disable(
                body.scope,
                body.target,
                actor=actor,
                tenant_id=kill_tenant,
            )
    except KillSwitchValidationError as exc:
        raise HTTPException(
            status_code=400, detail={"reason": "killswitch_invalid", "detail": exc.reason}
        ) from exc
    except KillSwitchUnavailable as exc:
        logger.warning(
            "admin.toggle_unavailable",
            error_code="killswitch_unavailable",
            trace_id=_trace_id(),
            scope=body.scope,
            action=body.action,
            reason=exc.reason,
        )
        raise HTTPException(
            status_code=503, detail={"reason": "killswitch_unavailable"}
        ) from exc
    except KillSwitchError as exc:
        logger.error(
            "admin.toggle_failed",
            error_code="killswitch_error",
            trace_id=_trace_id(),
            scope=body.scope,
            action=body.action,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail={"reason": "admin_error"}) from exc
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("toggle_set", exc) from exc

    audited = await _record_toggle(
        tenant=tenant,
        actor=actor,
        scope=body.scope,
        target=body.target,
        action=body.action,
        reason=body.reason,
    )
    envelope = _ok() if audited else _degrade("audit_append_failed")
    return {
        "ok": True,
        "action": body.action,
        "scope": body.scope,
        "target": body.target,
        "tenant_id": kill_tenant,
        "changed": bool(changed),
        "state": state,
        "audit_recorded": audited,
        **envelope,
    }


__all__ = ["router", "require_admin"]

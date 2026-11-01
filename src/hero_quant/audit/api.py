"""audit.api — 审计查询路由 + 链A最小生产者（PR-171）。

职责：
- `GET /v1/audit`：只读查询当前租户的审计事件（按 run_id 查全链路 / 按时间或用户检索）；
- `GET /v1/audit/verify`：重算链式哈希校验完整性（PR-241，只读、不修复、不回填）；
- `record_query_audit()`：`/v1/query` 与 `/v1/query/stream` 收尾时写**一条**审计事件。

架构位置：audit 包自身的 FastAPI 接缝；server.py 只 `include_router` + 调生产者，
查询/写入逻辑都在本模块，避免 server.py 继续膨胀。

关键设计（fail-closed，绝不假成功）：
- 租户只从 contextvars 取（`require_tenant()`）；缺失即 503 `auth_required_for_audit`，
  绝不回退 default 租户，也**不接受 query 参数覆盖租户**（跨租户只能由上下文决定）；
- 角色闸复用 `auth.deps.require_role("admin")` 的判定（等级包含，admin 通吃），
  但无主体时不在此处 401 —— 统一交给 `_audit_scope()` 出 503，与 `/v1/sessions` 同口径；
- 存储异常分级映射：不可用 503 / 入参非法 400 / 其余 500 只回通用 code，细节进 structlog；
- 生产者只落计数与 hash：prompt 原文/答案正文永不进审计，`prompt_hash` 用 sha256 前 32 hex；
- 生产者写入走 `AuditChain`（PR-241 入链，ts 由应用显式生成）；写失败只 warning，
  **绝不改变答案与状态码**；租户为空/无 trace_id 即跳过。
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Any

import structlog
import structlog.contextvars
from fastapi import APIRouter, Depends, HTTPException, Query

from hero_quant.audit.chain import AuditChain
from hero_quant.audit.store import (
    AuditError,
    AuditStoreUnavailable,
    AuditValidationError,
    get_audit_store,
)
from hero_quant.auth.context import current_principal, current_tenant_id, require_tenant
from hero_quant.auth.deps import require_role
from hero_quant.auth.errors import InsufficientRole, MissingTenant

logger = structlog.get_logger("audit")

router = APIRouter()

# API 层比 store 更严：store 允许到 1000，对外硬上限 500（一次查询不拖垮 PG/序列化）。
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
# prompt 只落 sha256 前 32 hex：够对账（同 prompt 同 hash），不可还原。
_PROMPT_HASH_CHARS = 32
_MAX_DEGRADED_REASONS = 16


# ---------------------------------------------------------------------------
# 身份 / 租户闸
# ---------------------------------------------------------------------------
_admin_role_dependency = require_role("admin")


async def require_audit_admin() -> None:
    """审计查询的 admin 闸：已绑定主体必须是 admin；未绑定主体放行到 503（绝不匿名放行）。

    中文：判定逻辑直接复用 `auth.deps.require_role("admin")`，只把 `InsufficientRole`
    显式映射成 403——本路由可能挂在没装 `install_auth_error_handlers` 的 app（测试/嵌入场景），
    403 不能依赖全局异常处理器才成立。无主体时不在这里 401：`_audit_scope()` 会统一出
    503 `auth_required_for_audit`，避免"没库/没租户"被报成"没登录"。
    """
    principal = current_principal()
    if principal is None:
        return
    try:
        await _admin_role_dependency(principal)
    except InsufficientRole as exc:
        raise HTTPException(status_code=403, detail={"reason": "audit_requires_admin"}) from exc


def _audit_scope() -> str:
    """当前请求租户；未认证/未绑定即 503，绝不回退 default（与 /v1/sessions 同口径）。"""
    try:
        return require_tenant()
    except MissingTenant as exc:
        raise HTTPException(status_code=503, detail={"reason": "auth_required_for_audit"}) from exc


# ---------------------------------------------------------------------------
# 入参归一
# ---------------------------------------------------------------------------
def _clamp_limit(value: Any) -> int:
    """limit 夹到 [1, MAX_LIMIT]：超上限不报错而是夹住（调用方拿得到可用的上限值）。"""
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise AuditValidationError("limit_must_be_int") from exc
    return max(1, min(number, MAX_LIMIT))


def _clamp_offset(value: Any) -> int:
    """offset < 0 归 0（负数偏移无意义，但也不必为此报错打断调用方）。"""
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise AuditValidationError("offset_must_be_int") from exc
    return max(0, number)


def _parse_instant(value: str | None, field: str) -> datetime | None:
    """ISO 时间或日期字符串 -> datetime；非法格式在**落库前**拒绝（400，不是 PG 报错变 500）。

    中文：纯日期按当日 00:00 解释（不偷偷扩成当天末尾——区间语义与字面一致）。
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise AuditValidationError(f"{field}_must_be_iso_datetime_or_date") from exc


def _trace_id() -> str | None:
    """当前请求 trace_id（= X-Request-ID，由 server 中间件绑到 contextvars）。

    中文：拿不到就返回 None，由调用方跳过本条审计——绝不 uuid 现编，
    否则审计里的 run_id 与后端日志/X-Request-ID 对不上，等于自造假链路。
    """
    value = structlog.contextvars.get_contextvars().get("trace_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


# ---------------------------------------------------------------------------
# GET /v1/audit
# ---------------------------------------------------------------------------
@router.get("/v1/audit")
async def query_audit(
    run_id: str | None = Query(None, description="给定即返回该链路全量事件（时间正序，等价 store.trace）"),
    user_id: str | None = Query(None, description="按操作人过滤；租户恒取当前上下文，不接受覆盖"),
    action: str | None = Query(None, description="按 action 精确过滤，如 query.completed"),
    since: str | None = Query(None, description="ISO 时间或日期（闭区间下界）"),
    until: str | None = Query(None, description="ISO 时间或日期（闭区间上界）"),
    limit: int = Query(DEFAULT_LIMIT, description=f"默认 {DEFAULT_LIMIT}，硬上限 {MAX_LIMIT}（超出被夹到上限）"),
    offset: int = Query(0, description="偏移，<0 归 0；run_id 全链路模式一次返回，不分页"),
    _admin: None = Depends(require_audit_admin),
) -> dict[str, Any]:
    """查询当前租户的审计事件（只读）。

    排序口径由 store 决定：给 `run_id` 走 `trace()`（时间正序，便于串联一条链路）；
    不给则走 `query()`（时间倒序，便于"最近发生了什么"）。响应恒回
    `{"items", "count", "limit", "offset"}`，item 原样透出 store 行（ts 由 FastAPI 序列化）。
    """
    tenant = _audit_scope()
    try:
        limit_n = _clamp_limit(limit)
        offset_n = _clamp_offset(offset)
        store = get_audit_store()
        if run_id is not None:
            items = await asyncio.to_thread(store.trace, tenant, run_id, limit=limit_n)
        else:
            items = await asyncio.to_thread(
                store.query,
                tenant,
                user_id=user_id,
                action=action,
                since=_parse_instant(since, "since"),
                until=_parse_instant(until, "until"),
                limit=limit_n,
                offset=offset_n,
            )
    except AuditStoreUnavailable as exc:
        logger.warning(
            "audit.query_unavailable",
            error_code="audit_store_unavailable",
            trace_id=_trace_id(),
            reason=exc.reason,
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail={"reason": "audit_store_unavailable"}) from exc
    except AuditValidationError as exc:
        logger.warning(
            "audit.query_invalid",
            error_code="audit_invalid",
            trace_id=_trace_id(),
            reason=exc.reason,
        )
        raise HTTPException(
            status_code=400, detail={"reason": "audit_invalid", "detail": exc.reason}
        ) from exc
    except AuditError as exc:
        # 中文：对外只回通用 code；reason/异常类型进日志（审计表里有 tenant 维度，脱敏即可）。
        logger.error(
            "audit.query_failed",
            error_code="audit_error",
            trace_id=_trace_id(),
            reason=exc.reason,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail={"reason": "audit_error"}) from exc
    except Exception as exc:  # noqa: BLE001 — 驱动层异常（psycopg/池）也必须脱敏成通用 500
        logger.error(
            "audit.query_failed",
            error_code="audit_error",
            trace_id=_trace_id(),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail={"reason": "audit_error"}) from exc
    return {"items": items, "count": len(items), "limit": limit_n, "offset": offset_n}


# ---------------------------------------------------------------------------
# GET /v1/audit/verify（PR-241 链式哈希校验）
# ---------------------------------------------------------------------------
@router.get("/v1/audit/verify")
async def verify_audit_chain(
    limit: int | None = Query(
        None,
        description=f"可选：只校验最早的 N 条（1..{MAX_LIMIT}）；缺省全量重算，耗时随行数线性增长",
    ),
    _admin: None = Depends(require_audit_admin),
) -> dict[str, Any]:
    """校验当前租户审计链的完整性（只读）。篡改是**返回值**，不是异常。

    响应字段（与 AuditChain.verify 一致）：
    ok / entries / head_hash / broken_at / reason / degraded / reason_code
    + chain_start_entry_id / chain_start_at / uncovered_count。

    中文口径：迁移前历史行的 prev_hash/entry_hash 为 NULL，**不回填、不伪造**；
    链自 chain_start_entry_id（ts=chain_start_at）起算，此前 uncovered_count 条未覆盖。
    文档/前端禁止把它写成「审计已启用」这类全量断言。
    """
    tenant = _audit_scope()
    try:
        store = get_audit_store()
        chain = AuditChain(store)
        if limit is None:
            report = await asyncio.to_thread(chain.verify, tenant)
        else:
            report = await asyncio.to_thread(chain.verify, tenant, limit=_clamp_limit(limit))
    except AuditStoreUnavailable as exc:
        logger.warning(
            "audit.verify_unavailable",
            error_code="audit_store_unavailable",
            trace_id=_trace_id(),
            reason=exc.reason,
            error_type=type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail={"reason": "audit_store_unavailable"}) from exc
    except AuditValidationError as exc:
        logger.warning(
            "audit.verify_invalid",
            error_code="audit_invalid",
            trace_id=_trace_id(),
            reason=exc.reason,
        )
        raise HTTPException(
            status_code=400, detail={"reason": "audit_invalid", "detail": exc.reason}
        ) from exc
    except AuditError as exc:
        logger.error(
            "audit.verify_failed",
            error_code="audit_error",
            trace_id=_trace_id(),
            reason=exc.reason,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail={"reason": "audit_error"}) from exc
    except Exception as exc:  # noqa: BLE001 — 驱动层异常（psycopg/池）也必须脱敏成通用 500
        logger.error(
            "audit.verify_failed",
            error_code="audit_error",
            trace_id=_trace_id(),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail={"reason": "audit_error"}) from exc
    return report


# ---------------------------------------------------------------------------
# 链A最小生产者（best-effort）
# ---------------------------------------------------------------------------
def _prompt_digest(prompt: str | None) -> str | None:
    """prompt 只落 sha256 前 32 hex；空/非字符串不产 hash（不写空串占位）。"""
    if not isinstance(prompt, str) or not prompt:
        return None
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:_PROMPT_HASH_CHARS]


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _build_detail(
    *,
    cost: dict[str, Any] | None,
    degraded: list[str] | None,
    iterations: int | None,
    grounded: bool | None,
    error_type: str | None,
) -> dict[str, Any]:
    """只放计数/标志：token 三计数、cost_usd、降级原因码、轮次、接地标志。

    中文：失败路径只落 `error_code` + 异常**类型名**——异常原文可能夹带 prompt/答案片段，
    一律不写（`store._safe_detail` 的敏感键黑名单拦不住自由文本里的引用内容）。
    """
    if error_type is not None:
        return {"error_code": "internal_error", "error_type": error_type}
    tokens: dict[str, int | None] | None = None
    cost_usd: float | None = None
    if isinstance(cost, dict):
        tokens = {
            "input_tokens": _as_int(cost.get("input_tokens")),
            "output_tokens": _as_int(cost.get("output_tokens")),
            "total_tokens": _as_int(cost.get("total_tokens")),
        }
        raw_cost = cost.get("cost_usd")
        if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool):
            cost_usd = float(raw_cost)
    return {
        "tokens": tokens,
        "cost_usd": cost_usd,
        "degraded": [str(reason) for reason in (degraded or [])][:_MAX_DEGRADED_REASONS],
        "iterations": _as_int(iterations),
        "grounded": grounded if isinstance(grounded, bool) else None,
    }


def _append(
    tenant: str,
    action: str,
    *,
    user_id: str | None,
    resource: str | None,
    run_id: str,
    model: str | None,
    prompt_hash: str | None,
    detail: dict[str, Any],
) -> bool:
    """线程内执行：取 store + 入链追加，任何异常在此收敛成 False + warning。

    中文：query.* 也走 AuditChain（PR-241），否则这些行 entry_hash 为 NULL、天天推高
    verify 的 uncovered_count。ts 必须由这里显式给出并参与 hash —— 交给 DB 的 now()
    生成，校验端重算就对不上。失败语义与旧的 try_append 一致：只降级、不阻断响应。
    """
    try:
        store = get_audit_store()
    except AuditError as exc:
        logger.warning(
            "audit.producer_skipped",
            error_code="audit_store_unavailable",
            action=action,
            reason=exc.reason,
        )
        return False
    except Exception as exc:  # noqa: BLE001 — 建 store 的其它失败（配置/驱动）同样只降级
        logger.warning(
            "audit.producer_skipped",
            error_code="audit_store_unavailable",
            action=action,
            error_type=type(exc).__name__,
        )
        return False
    try:
        AuditChain(store).append(
            tenant,
            action,
            user_id=user_id,
            resource=resource,
            run_id=run_id,
            model=model,
            prompt_hash=prompt_hash,
            detail=detail,
            ts=datetime.now(timezone.utc),
        )
    except Exception as exc:  # noqa: BLE001 — 审计是旁路：未预期异常绝不允许冒到响应
        logger.warning(
            "audit.append_failed",
            error_code="audit_append_failed",
            action=action,
            error_type=type(exc).__name__,
        )
        return False
    return True


async def record_query_audit(
    action: str,
    *,
    resource: str,
    model: str | None = None,
    prompt: str | None = None,
    cost: dict[str, Any] | None = None,
    degraded: list[str] | None = None,
    iterations: int | None = None,
    grounded: bool | None = None,
    error_type: str | None = None,
) -> bool:
    """写一条链A审计（best-effort）：返回是否落库；**任何失败都不外抛**。

    跳过条件（都打 warning，不静默）：
    - 租户为空（auth 未启用）：审计必须有 tenant，没有就不写；
    - trace_id 缺失：绝不现编 uuid 冒充 run_id（会和后端日志对不上）。
    存储 IO 走 `asyncio.to_thread`，不阻塞事件循环。
    """
    tenant = current_tenant_id()
    if not tenant:
        logger.debug("audit.producer_skipped", error_code="audit_tenant_missing", action=action)
        return False
    run_id = _trace_id()
    if run_id is None:
        logger.warning("audit.producer_skipped", error_code="audit_trace_id_missing", action=action)
        return False
    principal = current_principal()
    detail = _build_detail(
        cost=cost,
        degraded=degraded,
        iterations=iterations,
        grounded=grounded,
        error_type=error_type,
    )
    try:
        return await asyncio.to_thread(
            _append,
            tenant,
            action,
            user_id=getattr(principal, "subject", None),
            resource=resource,
            run_id=run_id,
            model=model,
            prompt_hash=_prompt_digest(prompt),
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001 — 线程池不可用等：审计失败绝不改响应
        logger.warning(
            "audit.producer_failed",
            error_code="audit_producer_failed",
            action=action,
            error_type=type(exc).__name__,
        )
        return False


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "record_query_audit",
    "require_audit_admin",
    "router",
]

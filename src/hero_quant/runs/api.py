"""runs.api — /v1/runs 队列接口 + SSE 进度流（PR-201/202/203）。

挂载（主控）：`app.include_router(runs.api.router, prefix="/v1/runs")` 与
`prefix="/runs"` 双注册；本模块内的路径都是**相对**的。

设计：
- 租户只从 contextvars 取（`require_tenant()`），缺租户 503，绝不回退默认租户；
- 每个端点取到连接后都会经过 `runs.guards.assert_no_queue_worker`（在 RunQueue 内），
  连接上残留 worker GUC 直接 503，不静默继续；
- SSE 事件来源是轮询 `runs` + `run_steps`（不新增事件表），事件名固定为
  run.queued / run.running / run.progress / run.done / run.failed / run.cancelled，
  每 15s 一条 `: keep-alive` 注释行。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from hero_quant.auth.context import current_principal, require_tenant
from hero_quant.auth.deps import require_role
from hero_quant.auth.errors import InsufficientRole, MissingTenant
from hero_quant.runs.guards import QueueWorkerOnApiConnection
from hero_quant.runs.steps import StepLedger
from hero_quant.runs.store import (
    RUN_STATUSES,
    TERMINAL_STATUSES,
    RunNotFound,
    RunQueue,
    RunsError,
    RunsUnavailable,
    RunsValidationError,
)

router = APIRouter()

_EVENT_POLL_SECONDS = 1.0
_KEEP_ALIVE_SECONDS = 15.0
_DEFAULT_LIST_LIMIT = 50
_DEFAULT_MAX_TRIES = 3

_EVENT_BY_STATUS = {
    "queued": "run.queued",
    "running": "run.running",
    "cancelling": "run.running",
    "succeeded": "run.done",
    "failed": "run.failed",
    "cancelled": "run.cancelled",
    "dead": "run.failed",
}

_viewer_dependency = require_role("viewer")

_queue_lock = threading.Lock()
_queue: RunQueue | None = None


# ---------------------------------------------------------------------------
# 队列单例（测试可替换）
# ---------------------------------------------------------------------------
def get_run_queue() -> RunQueue:
    """模块级 RunQueue 单例；首用时按 Settings.checkpoint_dsn 建池（fail-closed）。"""
    global _queue
    with _queue_lock:
        if _queue is None:
            _queue = RunQueue.from_dsn()
        return _queue


def set_run_queue(queue: RunQueue | None) -> None:
    """替换单例（测试/嵌入场景）；传 None 等价 reset。"""
    global _queue
    with _queue_lock:
        _queue = queue


def reset_run_queue() -> None:
    global _queue
    with _queue_lock:
        _queue = None


# ---------------------------------------------------------------------------
# 身份 / 租户闸
# ---------------------------------------------------------------------------
async def require_runs_access() -> None:
    """队列接口的 viewer 闸：无主体时放行到 503（与 /v1/sessions 同口径）。

    中文：与 audit/api.py 同一写法 —— 已绑定主体必须是 viewer 及以上，未绑定主体不在
    这里 401，统一交给 `_run_scope()` 出 503 `auth_required_for_runs`，避免「没库/没租户」
    被报成「没登录」。
    """
    principal = current_principal()
    if principal is None:
        return
    try:
        await _viewer_dependency(principal)
    except InsufficientRole as exc:
        raise HTTPException(status_code=403, detail={"reason": "runs_requires_viewer"}) from exc


def _run_scope() -> str:
    try:
        return require_tenant()
    except MissingTenant as exc:
        raise HTTPException(
            status_code=503, detail={"reason": "auth_required_for_runs"}
        ) from exc


def _http_error(exc: Exception) -> HTTPException:
    """队列错误 -> HTTP：404 不存在、503 不可用/GUC 泄漏、400 入参非法。"""
    if isinstance(exc, QueueWorkerOnApiConnection):
        # 池被 worker/API 混用：这类连接可能跨租户可见，宁可整体 503 也不继续服务。
        return HTTPException(status_code=503, detail={"reason": exc.error})
    if isinstance(exc, RunNotFound):
        return HTTPException(status_code=404, detail={"reason": "run_not_found"})
    if isinstance(exc, RunsUnavailable):
        return HTTPException(status_code=503, detail={"reason": exc.reason})
    if isinstance(exc, RunsValidationError):
        return HTTPException(
            status_code=400, detail={"reason": "run_invalid_request", "detail": exc.reason}
        )
    if isinstance(exc, RunsError):
        return HTTPException(status_code=500, detail={"reason": exc.reason})
    return HTTPException(status_code=500, detail={"reason": "runs_error"})


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------
class EnqueueRunRequest(BaseModel):
    kind: str = Field(default="agent", description="agent / backtest / research")
    payload: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None
    max_tries: int = _DEFAULT_MAX_TRIES
    run_id: str | None = Field(default=None, description="可选：客户端提供用于幂等，重复即 400")


# ---------------------------------------------------------------------------
# POST /v1/runs
# ---------------------------------------------------------------------------
@router.post("", status_code=201)
async def enqueue_run(
    body: EnqueueRunRequest,
    _authz: None = Depends(require_runs_access),
) -> dict[str, Any]:
    """入队一个长任务；返回 queued 状态的 run 行。"""
    tenant = _run_scope()
    try:
        row = await asyncio.to_thread(
            get_run_queue().enqueue,
            tenant,
            body.kind,
            body.payload,
            session_id=body.session_id,
            max_tries=body.max_tries,
            run_id=body.run_id,
        )
    except Exception as exc:  # noqa: BLE001 - 统一映射，细节进日志
        raise _http_error(exc) from exc
    return {"run": row}


# ---------------------------------------------------------------------------
# GET /v1/runs
# ---------------------------------------------------------------------------
@router.get("")
async def list_runs(
    status: str | None = Query(None, description=f"可选，取值 {'/'.join(sorted(RUN_STATUSES))}"),
    limit: int = Query(_DEFAULT_LIST_LIMIT, description="默认 50，硬上限 200（超出被夹住）"),
    _authz: None = Depends(require_runs_access),
) -> dict[str, Any]:
    """当前租户的 run 列表（created_at 倒序）。"""
    tenant = _run_scope()
    try:
        items = await asyncio.to_thread(get_run_queue().list, tenant, status=status, limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc
    return {"items": items, "count": len(items)}


# ---------------------------------------------------------------------------
# GET /v1/runs/{run_id}
# ---------------------------------------------------------------------------
@router.get("/{run_id}")
async def get_run(
    run_id: str,
    _authz: None = Depends(require_runs_access),
) -> dict[str, Any]:
    """run 详情 + step 断点 + 进度；跨租户表现成 404。"""
    tenant = _run_scope()
    queue = get_run_queue()
    try:
        row = await asyncio.to_thread(queue.get, tenant, run_id)
        steps = await asyncio.to_thread(StepLedger(queue.pool, tenant_id=tenant)._list_steps, run_id)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc
    done = sum(1 for step in steps if step.get("status") == "succeeded")
    return {
        "run": row,
        "steps": steps,
        "progress": {"done_steps": done, "total_steps": _declared_total_steps(row)},
    }


# ---------------------------------------------------------------------------
# POST /v1/runs/{run_id}/cancel
# ---------------------------------------------------------------------------
@router.post("/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    _authz: None = Depends(require_runs_access),
) -> dict[str, Any]:
    """queued → cancelled；running → cancelling（worker 在 step 边界收成 cancelled）。"""
    tenant = _run_scope()
    try:
        row = await asyncio.to_thread(get_run_queue().request_cancel, tenant, run_id)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc) from exc
    cancelled = bool(row.pop("cancelled", False))
    return {"run": row, "cancelled": cancelled}


# ---------------------------------------------------------------------------
# GET /v1/runs/{run_id}/events（SSE）
# ---------------------------------------------------------------------------
@router.get("/{run_id}/events")
async def stream_run_events(
    run_id: str,
    request: Request,
    _authz: None = Depends(require_runs_access),
) -> StreamingResponse:
    """SSE 进度流；首个事件反映当前状态，终态后关闭。"""
    tenant = _run_scope()
    queue = get_run_queue()
    try:
        initial = await asyncio.to_thread(queue.get, tenant, run_id)
    except Exception as exc:  # noqa: BLE001 - 流启动前必须是正常 HTTP 错误
        raise _http_error(exc) from exc
    return StreamingResponse(
        _event_stream(tenant, run_id, initial, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _declared_total_steps(row: dict[str, Any]) -> int | None:
    """总步数只取 payload 里声明的值；没声明就是 None（不猜、不编）。"""
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return None
    hint = payload.get("total_steps")
    return int(hint) if isinstance(hint, int) and hint > 0 else None


def _sse(event: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {body}\n\n"


async def _event_stream(
    tenant: str,
    run_id: str,
    initial: dict[str, Any],
    request: Request,
) -> Any:
    """轮询 runs + run_steps 生成事件；不新增事件表（契约 §5）。"""
    queue = get_run_queue()
    ledger = StepLedger(queue.pool, tenant_id=tenant)
    row = initial
    last_status = ""
    last_done = -1
    last_keep = time.monotonic()
    while True:
        status = str(row.get("status") or "")
        try:
            steps = await asyncio.to_thread(ledger._list_steps, run_id)
        except RunsError:
            steps = []
        except QueueWorkerOnApiConnection:
            # 池已污染：立即终止流，不再读任何跨租户数据。
            yield _sse("run.failed", {"event": "run.failed", "run_id": run_id, "status": "unknown",
                                      "error": QueueWorkerOnApiConnection.error})
            return
        done = sum(1 for step in steps if step.get("status") == "succeeded")
        if status and status != last_status:
            event = _EVENT_BY_STATUS.get(status, "run.progress")
            payload: dict[str, Any] = {
                "event": event,
                "run_id": run_id,
                "status": status,
                "attempt": row.get("attempt"),
            }
            if status in {"failed", "dead"}:
                payload["error"] = row.get("error")
            yield _sse(event, payload)
            last_status = status
        if status == "running" and done != last_done:
            yield _sse(
                "run.progress",
                {
                    "event": "run.progress",
                    "run_id": run_id,
                    "step": steps[-1]["step_name"] if steps else None,
                    "done_steps": done,
                    "total_steps": _declared_total_steps(row),
                },
            )
        last_done = done
        if status in TERMINAL_STATUSES:
            return
        if time.monotonic() - last_keep >= _KEEP_ALIVE_SECONDS:
            yield ": keep-alive\n\n"
            last_keep = time.monotonic()
        if await request.is_disconnected():
            return
        await asyncio.sleep(_EVENT_POLL_SECONDS)
        try:
            row = await asyncio.to_thread(queue.get, tenant, run_id)
        except RunNotFound:
            return
        except RunsError:
            return


__all__ = [
    "EnqueueRunRequest",
    "get_run_queue",
    "require_runs_access",
    "reset_run_queue",
    "router",
    "set_run_queue",
]

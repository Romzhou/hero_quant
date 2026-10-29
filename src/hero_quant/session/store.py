"""session — 会话/消息/运行/工具调用持久化（W4 PR-131/132/134）。

设计要点：
- **tenant 硬边界**：每个方法都要求显式 tenant_id，每条 SQL 都带 `tenant_id = %s`；
  同一事务内 `set_config('app.tenant', ..., true)` 供 RLS FORCE 兜底。
  跨租户访问表现为「不存在」（SessionNotFound / RunNotFound），不泄露对方是否存在。
- **工具幂等**：`(tenant_id, idempotency_key)` 唯一；重复调用返回首次行且不覆盖结果。
  与 `governance/dedup.py` 的分工：DedupStore 是执行前占位（PENDING→终态，决定「要不要执行」），
  本表的 tool_calls 是执行后留痕（结果引用/错误），两者共用同一 idempotency_key。
- **续跑游标**：`messages.id` 单调递增，直接作为 SSE `Last-Event-ID`。
- **fail-closed**：无连接池时 `SessionStoreUnavailable`（API 应转 503），不做内存假成功。

调用方注意：本模块是同步 psycopg 语义，在 async 端点上应 `await asyncio.to_thread(...)`
（与 server.py 既有 checkpoint/billing 调用一致）。
"""

from __future__ import annotations

import secrets
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from hero_quant.db.pool import DatabaseUnavailable, get_pool
from hero_quant.db.rows import execute, fetch_all, fetch_one

try:  # 可选依赖：用于把连接池超时精确映射为 503（而非 500）
    from psycopg_pool import PoolTimeout as _PoolTimeout  # type: ignore
except ImportError:  # pragma: no cover - 正常安装 psycopg_pool 时不会走到
    _PoolTimeout = ()  # type: ignore[assignment]

SESSION_ID_BYTES = 24
RUN_ID_BYTES = 12

MESSAGE_ROLES = frozenset({"system", "user", "assistant", "tool"})
RUN_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "cancelled"})
TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
TOOL_STATUSES = frozenset({"PENDING", "SUCCESS", "FAILED"})

_SESSION_COLUMNS = "id, tenant_id, user_id, title, created_at, updated_at"
_MESSAGE_COLUMNS = "id, tenant_id, session_id, role, content, trace_id, created_at"
_RUN_COLUMNS = (
    "id, tenant_id, session_id, status, reason, cost, started_at, ended_at, created_at"
)
_TOOL_CALL_COLUMNS = (
    "id, tenant_id, run_id, tool, args_hash, idempotency_key, status, result_ref, error, "
    "created_at, updated_at"
)

_MAX_TENANT_LEN = 128
_MAX_ID_LEN = 128
_MAX_TOOL_LEN = 128
_MAX_KEY_LEN = 512
_MAX_TITLE_LEN = 512
_MAX_CONTENT_LEN = 1_000_000
_MAX_LIMIT = 500


class SessionError(RuntimeError):
    """会话存储错误基类。"""

    error = "session_error"
    status_code = 500

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        message = f"{self.error}: {reason}"
        if detail:
            message += f" ({detail})"
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": self.error, "reason": self.reason}


class SessionStoreUnavailable(SessionError):
    """无可用 PG（未配置/探活失败）—— 503。"""

    error = "sessions_unavailable"
    status_code = 503


class SessionNotFound(SessionError):
    """会话不存在或不属于该 tenant —— 404。"""

    error = "session_not_found"
    status_code = 404


class RunNotFound(SessionError):
    """运行不存在或不属于该 tenant —— 404。"""

    error = "run_not_found"
    status_code = 404


class ToolCallNotFound(SessionError):
    """工具调用记录不存在 —— 404。"""

    error = "tool_call_not_found"
    status_code = 404


def _require_text(value: Any, field: str, max_len: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a str")
    text = value.strip()
    if not text:
        raise ValueError(f"{field} must be non-empty")
    if len(text) > max_len:
        raise ValueError(f"{field} exceeds {max_len} chars")
    return text


def _require_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an int") from exc
    if value <= 0:
        raise ValueError("limit must be > 0")
    return min(value, _MAX_LIMIT)


def _clip(value: Any, max_len: int) -> str | None:
    """可选文本字段有界化：None 透传，其余转 str 截断（防错误原文/原因无界膨胀）。"""
    if value is None:
        return None
    return str(value)[:max_len]


class SessionStore:
    """PG 会话存储；构造需显式连接池（测试可注入），不隐式建池。"""

    def __init__(self, pool: Any) -> None:
        if pool is None or not callable(getattr(pool, "connection", None)):
            raise ValueError("pool must provide a callable .connection()")
        self._pool = pool

    @classmethod
    def from_dsn(cls, dsn: str | None = None) -> "SessionStore":
        """从 DSN / Settings.checkpoint_dsn 建池；不可用即 fail-closed。"""
        try:
            return cls(get_pool(dsn))
        except DatabaseUnavailable as exc:
            raise SessionStoreUnavailable(exc.reason, str(exc)) from exc

    # ------------------------------------------------------------------ 基础

    @contextmanager
    def _tx(self, tenant_id: str) -> Iterator[Any]:
        """单事务 + 事务级 tenant 上下文（RLS 判据）。"""
        tenant = _require_text(tenant_id, "tenant_id", _MAX_TENANT_LEN)
        try:
            ctx = self._pool.connection()
        except Exception as exc:  # noqa: BLE001 - 取连接失败一律 503
            raise SessionStoreUnavailable("pool_connection_failed", type(exc).__name__) from exc
        try:
            with ctx as conn:
                with conn.transaction():
                    execute(conn, "SELECT set_config('app.tenant', %s, true)", (tenant,))
                    execute(conn, "SELECT set_config('app.current_tenant', %s, true)", (tenant,))
                    yield conn
        except _PoolTimeout as exc:  # type: ignore[misc]
            raise SessionStoreUnavailable("pool_timeout", type(exc).__name__) from exc

    def health(self) -> bool:
        """探活：供 /ready 使用；失败返回 False（不抛）。"""
        try:
            with self._pool.connection() as conn:
                execute(conn, "SELECT 1")
            return True
        except Exception:  # noqa: BLE001 - 探针语义就是布尔
            return False

    # --------------------------------------------------------------- sessions

    def create_session(self, tenant_id: str, user_id: str, title: str = "") -> dict[str, Any]:
        """服务端生成 session_id（不接受客户端 id，防固定会话）。"""
        tenant = _require_text(tenant_id, "tenant_id", _MAX_TENANT_LEN)
        user = _require_text(user_id, "user_id", _MAX_ID_LEN)
        if not isinstance(title, str):
            raise ValueError("title must be a str")
        safe_title = title.strip()[:_MAX_TITLE_LEN]
        sid = secrets.token_urlsafe(SESSION_ID_BYTES)
        with self._tx(tenant) as conn:
            row = fetch_one(
                conn,
                f"INSERT INTO sessions (id, tenant_id, user_id, title) VALUES (%s, %s, %s, %s) "
                f"RETURNING {_SESSION_COLUMNS}",
                (sid, tenant, user, safe_title),
            )
        if row is None:
            raise SessionError("insert_returned_no_row")
        return row

    def get_session(self, tenant_id: str, session_id: str) -> dict[str, Any]:
        tenant = _require_text(tenant_id, "tenant_id", _MAX_TENANT_LEN)
        sid = _require_text(session_id, "session_id", _MAX_ID_LEN)
        with self._tx(tenant) as conn:
            row = fetch_one(
                conn,
                f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE tenant_id = %s AND id = %s",
                (tenant, sid),
            )
        if row is None:
            raise SessionNotFound("unknown session")
        return row

    def list_sessions(
        self,
        tenant_id: str,
        *,
        user_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        tenant = _require_text(tenant_id, "tenant_id", _MAX_TENANT_LEN)
        capped = _require_limit(limit)
        skip = max(0, int(offset))
        params: list[Any] = [tenant]
        where = "tenant_id = %s"
        if user_id is not None:
            where += " AND user_id = %s"
            params.append(_require_text(user_id, "user_id", _MAX_ID_LEN))
        params.extend([capped, skip])
        with self._tx(tenant) as conn:
            return fetch_all(
                conn,
                f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE {where} "
                "ORDER BY updated_at DESC, id DESC LIMIT %s OFFSET %s",
                tuple(params),
            )

    # --------------------------------------------------------------- messages

    def append_message(
        self,
        tenant_id: str,
        session_id: str,
        role: str,
        content: str,
        *,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        tenant = _require_text(tenant_id, "tenant_id", _MAX_TENANT_LEN)
        sid = _require_text(session_id, "session_id", _MAX_ID_LEN)
        if role not in MESSAGE_ROLES:
            raise ValueError(f"role must be one of {sorted(MESSAGE_ROLES)}")
        if not isinstance(content, str) or len(content) > _MAX_CONTENT_LEN:
            raise ValueError("content must be a str within size limit")
        trace = None if trace_id is None else _require_text(trace_id, "trace_id", _MAX_ID_LEN)
        with self._tx(tenant) as conn:
            row = fetch_one(
                conn,
                f"INSERT INTO messages (tenant_id, session_id, role, content, trace_id) "
                f"SELECT %s, %s, %s, %s, %s "
                f"WHERE EXISTS (SELECT 1 FROM sessions WHERE id = %s AND tenant_id = %s) "
                f"RETURNING {_MESSAGE_COLUMNS}",
                (tenant, sid, role, content, trace, sid, tenant),
            )
            if row is None:
                raise SessionNotFound("unknown session")
            execute(
                conn,
                "UPDATE sessions SET updated_at = now() WHERE id = %s AND tenant_id = %s",
                (sid, tenant),
            )
        return row

    def list_messages(
        self,
        tenant_id: str,
        session_id: str,
        *,
        after_id: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        tenant = _require_text(tenant_id, "tenant_id", _MAX_TENANT_LEN)
        sid = _require_text(session_id, "session_id", _MAX_ID_LEN)
        cursor = max(0, int(after_id))
        capped = _require_limit(limit)
        with self._tx(tenant) as conn:
            exists = fetch_one(
                conn,
                "SELECT 1 AS present FROM sessions WHERE tenant_id = %s AND id = %s",
                (tenant, sid),
            )
            if exists is None:
                raise SessionNotFound("unknown session")
            return fetch_all(
                conn,
                f"SELECT {_MESSAGE_COLUMNS} FROM messages "
                "WHERE tenant_id = %s AND session_id = %s AND id > %s ORDER BY id ASC LIMIT %s",
                (tenant, sid, cursor, capped),
            )

    # ------------------------------------------------------------------- runs

    def start_run(self, tenant_id: str, session_id: str) -> dict[str, Any]:
        tenant = _require_text(tenant_id, "tenant_id", _MAX_TENANT_LEN)
        sid = _require_text(session_id, "session_id", _MAX_ID_LEN)
        run_id = secrets.token_urlsafe(RUN_ID_BYTES)
        with self._tx(tenant) as conn:
            row = fetch_one(
                conn,
                f"INSERT INTO runs (id, tenant_id, session_id, status, started_at) "
                f"SELECT %s, %s, %s, 'running', now() "
                f"WHERE EXISTS (SELECT 1 FROM sessions WHERE id = %s AND tenant_id = %s) "
                f"RETURNING {_RUN_COLUMNS}",
                (run_id, tenant, sid, sid, tenant),
            )
        if row is None:
            raise SessionNotFound("unknown session")
        return row

    def finish_run(
        self,
        tenant_id: str,
        run_id: str,
        status: str,
        *,
        reason: str | None = None,
        cost: float | None = None,
    ) -> dict[str, Any]:
        tenant = _require_text(tenant_id, "tenant_id", _MAX_TENANT_LEN)
        rid = _require_text(run_id, "run_id", _MAX_ID_LEN)
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"status must be one of {sorted(TERMINAL_RUN_STATUSES)}")
        if cost is not None and (not isinstance(cost, (int, float)) or cost < 0):
            raise ValueError("cost must be a non-negative number")
        safe_reason = _clip(reason, 2000)
        with self._tx(tenant) as conn:
            row = fetch_one(
                conn,
                f"UPDATE runs SET status = %s, reason = %s, cost = COALESCE(%s, cost), ended_at = now() "
                f"WHERE tenant_id = %s AND id = %s RETURNING {_RUN_COLUMNS}",
                (status, safe_reason, cost, tenant, rid),
            )
        if row is None:
            raise RunNotFound("unknown run")
        return row

    def get_run(self, tenant_id: str, run_id: str) -> dict[str, Any]:
        tenant = _require_text(tenant_id, "tenant_id", _MAX_TENANT_LEN)
        rid = _require_text(run_id, "run_id", _MAX_ID_LEN)
        with self._tx(tenant) as conn:
            row = fetch_one(
                conn,
                f"SELECT {_RUN_COLUMNS} FROM runs WHERE tenant_id = %s AND id = %s",
                (tenant, rid),
            )
        if row is None:
            raise RunNotFound("unknown run")
        return row

    def list_runs(self, tenant_id: str, session_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        tenant = _require_text(tenant_id, "tenant_id", _MAX_TENANT_LEN)
        sid = _require_text(session_id, "session_id", _MAX_ID_LEN)
        capped = _require_limit(limit)
        with self._tx(tenant) as conn:
            return fetch_all(
                conn,
                f"SELECT {_RUN_COLUMNS} FROM runs WHERE tenant_id = %s AND session_id = %s "
                "ORDER BY created_at DESC, id DESC LIMIT %s",
                (tenant, sid, capped),
            )

    # ------------------------------------------------------------- tool calls

    def record_tool_call(
        self,
        tenant_id: str,
        run_id: str,
        tool: str,
        args_hash: str,
        idempotency_key: str,
        *,
        status: str = "PENDING",
        result_ref: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """记录一次工具调用；返回 (记录, 是否首次执行)。

        重复 idempotency_key 返回首次记录且不覆盖已有 status/result_ref——
        调用方据此跳过第二次副作用。
        """
        tenant = _require_text(tenant_id, "tenant_id", _MAX_TENANT_LEN)
        rid = _require_text(run_id, "run_id", _MAX_ID_LEN)
        tool_name = _require_text(tool, "tool", _MAX_TOOL_LEN)
        digest = _require_text(args_hash, "args_hash", _MAX_KEY_LEN)
        key = _require_text(idempotency_key, "idempotency_key", _MAX_KEY_LEN)
        if status not in TOOL_STATUSES:
            raise ValueError(f"status must be one of {sorted(TOOL_STATUSES)}")
        safe_ref = None if result_ref is None else _require_text(result_ref, "result_ref", _MAX_KEY_LEN)

        with self._tx(tenant) as conn:
            row = fetch_one(
                conn,
                f"INSERT INTO tool_calls "
                f"(tenant_id, run_id, tool, args_hash, idempotency_key, status, result_ref) "
                f"SELECT %s, %s, %s, %s, %s, %s, %s "
                f"WHERE EXISTS (SELECT 1 FROM runs WHERE id = %s AND tenant_id = %s) "
                f"ON CONFLICT (tenant_id, idempotency_key) DO NOTHING "
                f"RETURNING {_TOOL_CALL_COLUMNS}",
                (tenant, rid, tool_name, digest, key, status, safe_ref, rid, tenant),
            )
            if row is not None:
                return row, True
            existing = fetch_one(
                conn,
                f"SELECT {_TOOL_CALL_COLUMNS} FROM tool_calls "
                "WHERE tenant_id = %s AND idempotency_key = %s",
                (tenant, key),
            )
        if existing is None:
            # 既没有冲突行也没有可插入的 run：run 不存在或属于别的 tenant
            raise RunNotFound("unknown run")
        return existing, False

    def complete_tool_call(
        self,
        tenant_id: str,
        idempotency_key: str,
        status: str,
        *,
        result_ref: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """PENDING→终态；已终态时原样返回（幂等，不覆盖首次结果）。"""
        tenant = _require_text(tenant_id, "tenant_id", _MAX_TENANT_LEN)
        key = _require_text(idempotency_key, "idempotency_key", _MAX_KEY_LEN)
        if status not in {"SUCCESS", "FAILED"}:
            raise ValueError("status must be SUCCESS or FAILED")
        safe_ref = None if result_ref is None else _require_text(result_ref, "result_ref", _MAX_KEY_LEN)
        safe_error = _clip(error, 4000)
        with self._tx(tenant) as conn:
            row = fetch_one(
                conn,
                f"UPDATE tool_calls SET status = %s, result_ref = COALESCE(%s, result_ref), "
                f"error = %s, updated_at = now() "
                f"WHERE tenant_id = %s AND idempotency_key = %s AND status = 'PENDING' "
                f"RETURNING {_TOOL_CALL_COLUMNS}",
                (status, safe_ref, safe_error, tenant, key),
            )
            if row is None:
                row = fetch_one(
                    conn,
                    f"SELECT {_TOOL_CALL_COLUMNS} FROM tool_calls "
                    "WHERE tenant_id = %s AND idempotency_key = %s",
                    (tenant, key),
                )
        if row is None:
            raise ToolCallNotFound("unknown tool call")
        return row

    def get_tool_call(self, tenant_id: str, idempotency_key: str) -> dict[str, Any]:
        tenant = _require_text(tenant_id, "tenant_id", _MAX_TENANT_LEN)
        key = _require_text(idempotency_key, "idempotency_key", _MAX_KEY_LEN)
        with self._tx(tenant) as conn:
            row = fetch_one(
                conn,
                f"SELECT {_TOOL_CALL_COLUMNS} FROM tool_calls "
                "WHERE tenant_id = %s AND idempotency_key = %s",
                (tenant, key),
            )
        if row is None:
            raise ToolCallNotFound("unknown tool call")
        return row

    # ----------------------------------------------------------------- resume

    def resume_session(
        self,
        tenant_id: str,
        session_id: str,
        *,
        after_id: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        """重启/断线后续跑：返回会话 + 增量消息 + 最近 runs + SSE 游标。"""
        session = self.get_session(tenant_id, session_id)
        messages = self.list_messages(tenant_id, session_id, after_id=after_id, limit=limit)
        runs = self.list_runs(tenant_id, session_id)
        last_event_id = messages[-1]["id"] if messages else max(0, int(after_id))
        return {
            "session": session,
            "messages": messages,
            "runs": runs,
            "last_event_id": last_event_id,
            "degraded": False,
        }


_store: SessionStore | None = None
_store_lock = threading.Lock()


def get_session_store() -> SessionStore:
    """进程级单例（供 API 复用；测试用 reset_session_store 清理）。"""
    global _store
    with _store_lock:
        if _store is None:
            _store = SessionStore.from_dsn()
        return _store


def reset_session_store() -> None:
    global _store
    with _store_lock:
        _store = None


__all__ = [
    "MESSAGE_ROLES",
    "RUN_STATUSES",
    "RunNotFound",
    "SessionError",
    "SessionNotFound",
    "SessionStore",
    "SessionStoreUnavailable",
    "ToolCallNotFound",
    "get_session_store",
    "reset_session_store",
]

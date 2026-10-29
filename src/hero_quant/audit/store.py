"""audit.store — 只追加审计日志（W8 PR-171/173）。

约束：
- 表由 `db/migrations/0003_audit_log.sql` 创建，DB 触发器拒绝 UPDATE/DELETE；
  本模块**只提供 append/query**，没有改写/删除接口。
- 每条记录强制 tenant_id；查询一律带 `tenant_id = %s`，事务内 set_config 供 RLS 兜底。
- 只存 hash / 引用，不存 prompt、消息正文、结果原文；`detail` 命中敏感键直接拒绝。
- 审计不可静默失败：`append` 抛错由调用方决定阻断；需要 best-effort 的场景用
  `try_append`，它返回 False 并打 warning（不吞成成功）。
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator

from hero_quant.db.pool import DatabaseUnavailable, get_pool
from hero_quant.db.rows import execute, fetch_all, fetch_one

try:  # 可选依赖：连接池超时精确映射为 503
    from psycopg_pool import PoolTimeout as _PoolTimeout  # type: ignore
except ImportError:  # pragma: no cover
    _PoolTimeout = ()  # type: ignore[assignment]

_MAX_TENANT_LEN = 128
_MAX_TEXT_LEN = 256
_MAX_HASH_LEN = 128
_MAX_LIMIT = 1000

# 明文敏感键黑名单：审计只接受 hash/引用，正文落库即违规。
_SENSITIVE_DETAIL_KEYS = frozenset(
    {
        "prompt",
        "prompt_text",
        "content",
        "message",
        "messages",
        "result",
        "output",
        "api_key",
        "authorization",
        "password",
        "secret",
        "token",
    }
)

_AUDIT_COLUMNS = (
    "id, ts, tenant_id, user_id, action, resource, run_id, model, prompt_hash, result_hash, ip, detail"
)

_APPEND_SQL = (
    f"INSERT INTO audit_log (tenant_id, user_id, action, resource, run_id, model, prompt_hash, "
    f"result_hash, ip, detail) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
    f"RETURNING {_AUDIT_COLUMNS}"
)


class AuditError(RuntimeError):
    """审计写入/查询失败。"""

    error = "audit_error"
    status_code = 500

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        message = f"{self.error}: {reason}"
        if detail:
            message += f" ({detail})"
        super().__init__(message)


class AuditStoreUnavailable(AuditError):
    """无可用 PG —— 503。"""

    error = "audit_unavailable"
    status_code = 503


class AuditValidationError(AuditError):
    """入参违规（空 action / 明文敏感字段 / 超长）—— 400。"""

    error = "audit_invalid"
    status_code = 400


def _text(value: Any, field: str, max_len: int, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise AuditValidationError(f"{field}_required")
        return None
    if not isinstance(value, str):
        raise AuditValidationError(f"{field}_must_be_str")
    text = value.strip()
    if not text:
        if required:
            raise AuditValidationError(f"{field}_required")
        return None
    if len(text) > max_len:
        raise AuditValidationError(f"{field}_too_long")
    if "\n" in text or "\r" in text:
        raise AuditValidationError(f"{field}_must_be_single_line")
    return text


def _timestamp(value: Any, field: str) -> Any:
    if value is None:
        return None
    if isinstance(value, (datetime, date, str)):
        return value
    raise AuditValidationError(f"{field}_must_be_datetime_or_iso_string")


class AuditStore:
    """审计日志存储；构造需显式连接池。"""

    def __init__(self, pool: Any) -> None:
        if pool is None or not callable(getattr(pool, "connection", None)):
            raise ValueError("pool must provide a callable .connection()")
        self._pool = pool

    @classmethod
    def from_dsn(cls, dsn: str | None = None) -> "AuditStore":
        try:
            return cls(get_pool(dsn))
        except DatabaseUnavailable as exc:
            raise AuditStoreUnavailable(exc.reason, str(exc)) from exc

    @contextmanager
    def _tx(self, tenant_id: str) -> Iterator[Any]:
        tenant = _text(tenant_id, "tenant_id", _MAX_TENANT_LEN, required=True)
        try:
            ctx = self._pool.connection()
        except Exception as exc:  # noqa: BLE001 - 取连接失败一律 503
            raise AuditStoreUnavailable("pool_connection_failed", type(exc).__name__) from exc
        try:
            with ctx as conn:
                with conn.transaction():
                    execute(conn, "SELECT set_config('app.tenant', %s, true)", (tenant,))
                    execute(conn, "SELECT set_config('app.current_tenant', %s, true)", (tenant,))
                    yield conn
        except _PoolTimeout as exc:  # type: ignore[misc]
            raise AuditStoreUnavailable("pool_timeout", type(exc).__name__) from exc

    def append(
        self,
        tenant_id: str,
        action: str,
        *,
        user_id: str | None = None,
        resource: str | None = None,
        run_id: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        result_hash: str | None = None,
        ip: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """追加一条审计；返回落库后的完整行。"""
        tenant = _text(tenant_id, "tenant_id", _MAX_TENANT_LEN, required=True)
        act = _text(action, "action", _MAX_TEXT_LEN, required=True)
        payload = self._safe_detail(detail)
        # 所有入参必须在取连接之前校验完：非法输入应得 400，不能先撞 pool 拿到 503。
        actor_id = _text(user_id, "user_id", _MAX_TEXT_LEN, required=False)
        resource_text = _text(resource, "resource", _MAX_TEXT_LEN, required=False)
        run = _text(run_id, "run_id", _MAX_TEXT_LEN, required=False)
        model_text = _text(model, "model", _MAX_TEXT_LEN, required=False)
        prompt_digest = _text(prompt_hash, "prompt_hash", _MAX_HASH_LEN, required=False)
        result_digest = _text(result_hash, "result_hash", _MAX_HASH_LEN, required=False)
        client_ip = _text(ip, "ip", 64, required=False)
        with self._tx(tenant) as conn:
            row = fetch_one(
                conn,
                _APPEND_SQL,
                (
                    tenant,
                    actor_id,
                    act,
                    resource_text,
                    run,
                    model_text,
                    prompt_digest,
                    result_digest,
                    client_ip,
                    payload,
                ),
            )
        if row is None:
            raise AuditError("insert_returned_no_row")
        return row

    def try_append(self, tenant_id: str, action: str, **fields: Any) -> bool:
        """best-effort 追加：失败返回 False 并告警，绝不假装成功。"""
        try:
            self.append(tenant_id, action, **fields)
            return True
        except (AuditError, ValueError, TypeError) as exc:
            import logging

            logging.getLogger(__name__).warning(
                "audit.append_failed action=%s error_code=audit.append_failed error=%s",
                action,
                type(exc).__name__,
                exc_info=True,
            )
            return False

    def query(
        self,
        tenant_id: str,
        *,
        run_id: str | None = None,
        user_id: str | None = None,
        action: str | None = None,
        since: datetime | date | str | None = None,
        until: datetime | date | str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """按 tenant + 可选维度查询；默认时间倒序。"""
        tenant = _text(tenant_id, "tenant_id", _MAX_TENANT_LEN, required=True)
        clauses = ["tenant_id = %s"]
        params: list[Any] = [tenant]
        for column, value in (
            ("run_id", run_id),
            ("user_id", user_id),
            ("action", action),
        ):
            if value is not None:
                clauses.append(f"{column} = %s")
                params.append(_text(value, column, _MAX_TEXT_LEN, required=True))
        start = _timestamp(since, "since")
        if start is not None:
            clauses.append("ts >= %s")
            params.append(start)
        end = _timestamp(until, "until")
        if end is not None:
            clauses.append("ts <= %s")
            params.append(end)
        capped = max(1, min(int(limit), _MAX_LIMIT))
        params.extend([capped, max(0, int(offset))])
        sql = (
            f"SELECT {_AUDIT_COLUMNS} FROM audit_log WHERE {' AND '.join(clauses)} "
            "ORDER BY ts DESC, id DESC LIMIT %s OFFSET %s"
        )
        with self._tx(tenant) as conn:
            return fetch_all(conn, sql, tuple(params))

    def trace(self, tenant_id: str, run_id: str, *, limit: int = _MAX_LIMIT) -> list[dict[str, Any]]:
        """按 run_id 查全链路（时间正序，便于串联问答/工具/回测事件）。"""
        tenant = _text(tenant_id, "tenant_id", _MAX_TENANT_LEN, required=True)
        rid = _text(run_id, "run_id", _MAX_TEXT_LEN, required=True)
        capped = max(1, min(int(limit), _MAX_LIMIT))
        with self._tx(tenant) as conn:
            return fetch_all(
                conn,
                f"SELECT {_AUDIT_COLUMNS} FROM audit_log "
                "WHERE tenant_id = %s AND run_id = %s ORDER BY id ASC LIMIT %s",
                (tenant, rid, capped),
            )

    @staticmethod
    def _safe_detail(detail: dict[str, Any] | None) -> str:
        """校验 detail：必须是 dict，且不得出现明文敏感键；返回 JSON 字符串。"""
        if detail is None:
            return "{}"
        if not isinstance(detail, dict):
            raise AuditValidationError("detail_must_be_dict")
        lowered = {str(key).lower() for key in detail}
        hit = sorted(lowered & _SENSITIVE_DETAIL_KEYS)
        if hit:
            raise AuditValidationError(f"detail_contains_sensitive_keys:{','.join(hit)}")
        try:
            encoded = json.dumps(detail, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as exc:
            raise AuditValidationError("detail_not_json_serializable") from exc
        if len(encoded) > 8192:
            raise AuditValidationError("detail_too_large")
        return encoded


_store: AuditStore | None = None
_store_lock = threading.Lock()


def get_audit_store() -> AuditStore:
    """进程级单例（供 API/审计中间件复用）。"""
    global _store
    with _store_lock:
        if _store is None:
            _store = AuditStore.from_dsn()
        return _store


def reset_audit_store() -> None:
    global _store
    with _store_lock:
        _store = None


__all__ = [
    "AuditError",
    "AuditStore",
    "AuditStoreUnavailable",
    "AuditValidationError",
    "get_audit_store",
    "reset_audit_store",
]

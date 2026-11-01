"""admin.store — 管理后台只读聚合查询（PR-215）。

职责：
- 平台总览（各状态 run 计数 / 审计条数 / 最老 queued 等待秒数）；
- 租户清单（runs 数与最后活动时间，runs + audit_log 并集）；
- 单租户审计条数（审计摘要用）。

架构位置：admin 包的数据访问层；API 只做鉴权/降级映射，SQL 全部留在这里。

关键设计（与 `docs/contracts/batch-14.md` §1/§2.4 对齐）：
- **跨租户视图必须显式**：`runs` / `audit_log` 都是 FORCE RLS，只有 DB 角色
  具备 SUPERUSER/BYPASSRLS 时才看得见全平台。先用 `pg_roles` 判定，没权限就抛
  `AdminScopeUnavailable`，绝不让 RLS 把「无权限」过滤成「0 条」。
- 单租户查询沿用 `audit/store.py` 的事务级 `set_config` 口径（SET LOCAL，回池不残留）；
  SQL 仍显式带 `tenant_id = %s` 做第二道防线。
- 不可用一律抛 `AdminStoreUnavailable`，调用方转成 degraded/null，绝不返回 0 冒充真值。
- 日志只带 reason / error_type，不带 DSN、口令或行内容。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from hero_quant.db.pool import DatabaseUnavailable, get_pool
from hero_quant.db.rows import execute, fetch_all, fetch_one

try:  # 可选依赖：把池超时精确映射成 503
    from psycopg_pool import PoolTimeout as _PoolTimeout  # type: ignore
except ImportError:  # pragma: no cover - 正常安装 psycopg_pool 时不会走到
    _PoolTimeout = ()  # type: ignore[assignment]

logger = logging.getLogger(__name__)

MAX_TENANT_LEN = 128

_STATUS_COUNTS_SQL = "SELECT status, count(*) AS count FROM runs GROUP BY status"
_STATUS_COUNTS_TENANT_SQL = (
    "SELECT status, count(*) AS count FROM runs WHERE tenant_id = %s GROUP BY status"
)
_AUDIT_TOTAL_SQL = "SELECT count(*) AS total FROM audit_log"
_AUDIT_TOTAL_TENANT_SQL = "SELECT count(*) AS total FROM audit_log WHERE tenant_id = %s"
_OLDEST_QUEUED_SQL = (
    "SELECT id, created_at, EXTRACT(EPOCH FROM (now() - created_at)) AS wait_seconds "
    "FROM runs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
)
_OLDEST_QUEUED_TENANT_SQL = (
    "SELECT id, created_at, EXTRACT(EPOCH FROM (now() - created_at)) AS wait_seconds "
    "FROM runs WHERE status = 'queued' AND tenant_id = %s ORDER BY created_at ASC LIMIT 1"
)
# 租户清单：runs 与 audit_log 取并集，runs 数为 0 的租户（只有审计）也要出现。
_TENANTS_SQL = (
    "WITH run_stats AS ("
    "  SELECT tenant_id, count(*) AS runs, max(created_at) AS last_run_at"
    "  FROM runs GROUP BY tenant_id"
    "), audit_stats AS ("
    "  SELECT tenant_id, max(ts) AS last_audit_at FROM audit_log GROUP BY tenant_id"
    "), tenants AS ("
    "  SELECT tenant_id FROM run_stats UNION SELECT tenant_id FROM audit_stats"
    ") "
    "SELECT t.tenant_id, COALESCE(r.runs, 0) AS runs, "
    "GREATEST(r.last_run_at, a.last_audit_at) AS last_activity_at "
    "FROM tenants t "
    "LEFT JOIN run_stats r USING (tenant_id) "
    "LEFT JOIN audit_stats a USING (tenant_id) "
    "ORDER BY last_activity_at DESC NULLS LAST, t.tenant_id ASC LIMIT %s"
)
# RLS 对 SUPERUSER / BYPASSRLS 不生效；只有这两类角色能看跨租户全量。
_CROSS_TENANT_ALLOWED_SQL = (
    "SELECT COALESCE(bool_or(rolsuper OR rolbypassrls), false) AS allowed "
    "FROM pg_roles WHERE rolname = current_user"
)


class AdminError(RuntimeError):
    """管理后台查询错误基类。"""

    error = "admin_error"
    status_code = 500

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"{self.error}: {reason}")


class AdminStoreUnavailable(AdminError):
    """PG 不可用（未配置 / 驱动缺失 / 池超时）—— API 层应降级为 null。"""

    error = "admin_unavailable"
    status_code = 503


class AdminScopeUnavailable(AdminError):
    """跨租户视图被 RLS 拒绝（当前 DB 角色无 SUPERUSER/BYPASSRLS）。"""

    error = "admin_scope_unavailable"
    status_code = 503


def _tenant_arg(tenant_id: Any) -> str:
    """租户参数校验：空值直接拒绝，绝不回退默认租户。"""
    if not isinstance(tenant_id, str):
        raise AdminError("tenant_id_must_be_str")
    tenant = tenant_id.strip()
    if not tenant:
        raise AdminError("tenant_id_required")
    if len(tenant) > MAX_TENANT_LEN:
        raise AdminError("tenant_id_too_long")
    return tenant


class AdminStore:
    """只读管理查询；构造需显式连接池（测试可注入假池）。"""

    def __init__(self, pool: Any) -> None:
        if pool is None or not callable(getattr(pool, "connection", None)):
            raise ValueError("pool must provide a callable .connection()")
        self._pool = pool

    @classmethod
    def from_settings(cls) -> "AdminStore":
        """按 Settings.checkpoint_dsn 建/复用共享池；不可用即 fail-closed。"""
        try:
            return cls(get_pool())
        except DatabaseUnavailable as exc:
            logger.warning(
                "admin.store_unavailable error_code=pg_unavailable reason=%s", exc.reason
            )
            raise AdminStoreUnavailable("pg_unavailable") from exc

    @contextmanager
    def _tx(self, tenant_id: str | None) -> Iterator[Any]:
        """取连接并开事务；tenant_id 为 None 表示跨租户视图（不设 GUC）。"""
        try:
            ctx = self._pool.connection()
        except Exception as exc:  # noqa: BLE001 — 取连接失败一律按 PG 不可用处理
            logger.warning(
                "admin.store_unavailable error_code=pg_unavailable stage=connection error_type=%s",
                type(exc).__name__,
            )
            raise AdminStoreUnavailable("pg_unavailable") from exc
        try:
            with ctx as conn:
                with conn.transaction():
                    if tenant_id is not None:
                        tenant = _tenant_arg(tenant_id)
                        execute(conn, "SELECT set_config('app.tenant', %s, true)", (tenant,))
                        execute(
                            conn,
                            "SELECT set_config('app.current_tenant', %s, true)",
                            (tenant,),
                        )
                    yield conn
        except AdminError:
            raise
        except _PoolTimeout as exc:  # type: ignore[misc]
            logger.warning("admin.store_unavailable error_code=pg_unavailable stage=pool_timeout")
            raise AdminStoreUnavailable("pg_unavailable") from exc
        except Exception as exc:  # noqa: BLE001 — 驱动层异常统一映射为 503（细节进日志）
            logger.warning(
                "admin.store_failed error_code=pg_unavailable error_type=%s",
                type(exc).__name__,
            )
            raise AdminStoreUnavailable("pg_unavailable") from exc

    @staticmethod
    def _assert_cross_tenant(conn: Any) -> None:
        """确认当前 DB 角色能跨租户读；否则显式拒绝（不能把 RLS 过滤当成空数据）。"""
        row = fetch_one(conn, _CROSS_TENANT_ALLOWED_SQL)
        if row is None or not row.get("allowed"):
            raise AdminScopeUnavailable("cross_tenant_not_permitted")

    # ------------------------------------------------------------------ 总览
    def overview(self, tenant_id: str | None) -> dict[str, Any]:
        """一次事务内取齐三类计数；tenant_id=None 时是平台视图。"""
        with self._tx(tenant_id) as conn:
            if tenant_id is None:
                self._assert_cross_tenant(conn)
                status_rows = fetch_all(conn, _STATUS_COUNTS_SQL)
                audit_row = fetch_one(conn, _AUDIT_TOTAL_SQL)
                oldest = fetch_one(conn, _OLDEST_QUEUED_SQL)
            else:
                tenant = _tenant_arg(tenant_id)
                status_rows = fetch_all(conn, _STATUS_COUNTS_TENANT_SQL, (tenant,))
                audit_row = fetch_one(conn, _AUDIT_TOTAL_TENANT_SQL, (tenant,))
                oldest = fetch_one(conn, _OLDEST_QUEUED_TENANT_SQL, (tenant,))
        by_status = {str(row["status"]): int(row["count"]) for row in status_rows}
        return {
            "runs": {"total": sum(by_status.values()), "by_status": by_status},
            "audit": {"entries": int(audit_row["total"]) if audit_row else 0},
            "queue": _oldest_payload(oldest),
        }

    # -------------------------------------------------------------- 租户清单
    def tenants(self, *, limit: int) -> list[dict[str, Any]]:
        """跨租户清单；无跨租户权限时抛 AdminScopeUnavailable。"""
        with self._tx(None) as conn:
            self._assert_cross_tenant(conn)
            rows = fetch_all(conn, _TENANTS_SQL, (int(limit),))
        return [
            {
                "tenant_id": row["tenant_id"],
                "runs": int(row["runs"]),
                "last_activity_at": row["last_activity_at"],
            }
            for row in rows
        ]

    # -------------------------------------------------------------- 审计条数
    def audit_total(self, tenant_id: str) -> int:
        """单租户审计总条数（含链外历史行）。"""
        tenant = _tenant_arg(tenant_id)
        with self._tx(tenant) as conn:
            row = fetch_one(conn, _AUDIT_TOTAL_TENANT_SQL, (tenant,))
        return int(row["total"]) if row else 0


def _oldest_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """最老 queued 行 -> 响应片段；无排队任务返回 None（不是 0 秒）。"""
    if row is None:
        return None
    wait = row.get("wait_seconds")
    return {
        "oldest_queued_run_id": row.get("id"),
        "oldest_queued_created_at": row.get("created_at"),
        "oldest_queued_wait_seconds": max(0.0, float(wait)) if wait is not None else None,
    }


__all__ = [
    "AdminError",
    "AdminScopeUnavailable",
    "AdminStore",
    "AdminStoreUnavailable",
    "MAX_TENANT_LEN",
]

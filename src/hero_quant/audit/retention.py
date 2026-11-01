"""audit.retention — 保留策略规划（PR-241），**只读**。

audit_log 有 append-only 触发器（0003），应用代码没有删除/归档通路；本模块只回答
「按当前策略，多少条已过保留期、最老/最新是哪天」，供运维与后台展示。

归档是人工步骤（契约要求写明）：
1. 由 DBA 在维护窗口显式 ALTER TABLE audit_log DISABLE TRIGGER audit_log_append_only_trigger；
2. 导出/搬移 ts 早于 cutoff 的行到归档存储，并保留 id/prev_hash/entry_hash 供后续复算；
3. 同事务内 DELETE 已归档行，再 ALTER TABLE audit_log ENABLE TRIGGER ...；
4. 归档后链会从下一个 still-present 行断开（prev_hash 指向已搬走的行），
   校验结果会显示 prev_mismatch —— 这是预期，不是篡改：归档必须在审计台账里另记一笔。
禁止在应用代码里绕过触发器做删除（本模块也没有实现）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from hero_quant.audit.store import (
    AuditValidationError,
    _MAX_TENANT_LEN,
    _text,
    get_audit_store,
)
from hero_quant.db.rows import fetch_one

_MAX_OLDER_THAN_DAYS = 36_500  # 100 年上界：防止误传超大值把 cutoff 算成"远古"

_PLAN_SQL = (
    "SELECT count(*) AS total_count, "
    "count(*) FILTER (WHERE ts < %s) AS archivable_count, "
    "min(ts) AS oldest_at, max(ts) AS newest_at "
    "FROM audit_log WHERE tenant_id = %s"
)


def _coerce_now(now: Any) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    if isinstance(now, date):
        return datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    raise AuditValidationError("now_must_be_datetime_or_date")


def retention_plan(
    tenant_id: str,
    *,
    older_than_days: int = 365,
    now: datetime | None = None,
) -> dict[str, Any]:
    """返回可归档范围与条数；**绝不删除任何行**（delete_performed 恒为 False）。

    中文：默认保留一年（契约口径）；cutoff = now - older_than_days。返回
    archivable_count 只是规划数字，实际归档需按模块 docstring 的人工步骤执行。
    """
    tenant = _text(tenant_id, "tenant_id", _MAX_TENANT_LEN, required=True)
    if isinstance(older_than_days, bool) or not isinstance(older_than_days, int):
        raise AuditValidationError("older_than_days_must_be_int")
    if older_than_days < 0 or older_than_days > _MAX_OLDER_THAN_DAYS:
        raise AuditValidationError("older_than_days_out_of_range")
    reference = _coerce_now(now)
    cutoff = reference - timedelta(days=older_than_days)

    store = get_audit_store()
    with store._tx(tenant) as conn:
        row = fetch_one(conn, _PLAN_SQL, (cutoff, tenant))
    row = row or {}
    return {
        "tenant_id": tenant,
        "older_than_days": older_than_days,
        "cutoff": cutoff.isoformat(),
        "total_count": int(row.get("total_count") or 0),
        "archivable_count": int(row.get("archivable_count") or 0),
        "oldest_at": row.get("oldest_at"),
        "newest_at": row.get("newest_at"),
        "delete_performed": False,
        "manual_archive_required": True,
    }


__all__ = ["retention_plan"]

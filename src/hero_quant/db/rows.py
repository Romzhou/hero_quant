"""db.rows — 手写 SQL 的行转换与执行小工具（无 ORM）。

只做三件事：
1. 兼容 psycopg 连接/游标两种执行形态；
2. 把 cursor.description + row 转成 dict（列名以数据库为准，不猜）；
3. 把 datetime / Decimal / UUID 转成 JSON 友好的值，供 API 直接序列化。
"""

from __future__ import annotations

import uuid as _uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def execute(conn: Any, sql: str, params: Any = None) -> Any:
    """执行 SQL 并返回游标（需要 rowcount 时用）。"""
    try:
        if params is None:
            return conn.execute(sql)
        return conn.execute(sql, params)
    except AttributeError:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur


def fetch_one(conn: Any, sql: str, params: Any = None) -> dict[str, Any] | None:
    cur = execute(conn, sql, params)
    if cur is None:
        return None
    row = cur.fetchone()
    if row is None:
        return None
    return _to_dict(cur, row)


def fetch_all(conn: Any, sql: str, params: Any = None) -> list[dict[str, Any]]:
    cur = execute(conn, sql, params)
    if cur is None:
        return []
    rows = cur.fetchall() or []
    return [_to_dict(cur, row) for row in rows]


def _to_dict(cur: Any, row: Any) -> dict[str, Any]:
    description = getattr(cur, "description", None) or []
    columns: list[str] = []
    for item in description:
        name = getattr(item, "name", None)
        columns.append(name if isinstance(name, str) else str(item[0]))
    values = list(row) if not isinstance(row, dict) else list(row.values())
    return {name: jsonable(value) for name, value in zip(columns, values)}


def jsonable(value: Any) -> Any:
    """递归转 JSON 原生类型；未知类型原样返回（psycopg 已给出合理类型）。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, _uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


__all__ = ["execute", "fetch_all", "fetch_one", "jsonable"]

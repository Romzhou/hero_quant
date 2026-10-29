"""db.runner — 迁移执行器（schema_version + 事务 + 咨询锁 + drift 检测）。

不变量：
1. **单源**：schema 变更只经 `db/migrations/*.sql`；已应用迁移内容被改动 = drift，必须报错。
2. **原子**：每个迁移一个事务；任一语句失败则整版回滚，schema_version 不前进。
3. **串行**：多实例同时启动时用 `pg_advisory_lock` 串行化，避免并发 DDL。
4. **可重复**：空库与旧库（已有内联 DDL 建好的表）都能 upgrade 成功（baseline 幂等）。

用法（运维）：
    python -m hero_quant.db status     # 当前版本 / 待应用 / drift
    python -m hero_quant.db upgrade    # 应用全部待执行迁移
    python -m hero_quant.db check      # 只做 drift 检查（CI 用，非 0 退出即失败）
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterable, Iterator
from typing import Any

from hero_quant.db.loader import Migration, load_migrations

SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
  version integer PRIMARY KEY CHECK (version > 0),
  name text NOT NULL,
  checksum text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now(),
  duration_ms integer NOT NULL DEFAULT 0 CHECK (duration_ms >= 0)
)
"""

_ADVISORY_LOCK_KEY = 770_001

_APPLIED_SQL = "SELECT version, name, checksum, applied_at FROM schema_version ORDER BY version"
_INSERT_VERSION_SQL = (
    "INSERT INTO schema_version (version, name, checksum, duration_ms) VALUES (%s, %s, %s, %s)"
)

# 迁移完成后必须存在的表（drift 检测口径：缺表即 CI 红，避免「版本对上但库是空的」）。
EXPECTED_TABLES: tuple[str, ...] = (
    "schema_version",
    "checkpoints",
    "checkpoints_legacy",
    "factors",
    "purchases",
    "dedup",
    "tool_call_dedup",
    "sessions",
    "messages",
    "runs",
    "tool_calls",
    "audit_log",
)


class MigrationError(RuntimeError):
    """迁移无法继续（配置 / 版本 / 执行错误）。"""


class MigrationDriftError(MigrationError):
    """已应用迁移被改写，或数据库 schema 与代码期望不一致。"""


def _execute(conn: Any, sql: str, params: Any = None) -> Any:
    """兼容 psycopg 连接 / 游标两种调用形态（仓库既有写法，见 billing/dedup）。"""
    try:
        if params is None:
            return conn.execute(sql)
        return conn.execute(sql, params)
    except AttributeError:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur


def _fetch_all(conn: Any, sql: str, params: Any = None) -> list[Any]:
    cur = _execute(conn, sql, params)
    if cur is None:
        return []
    try:
        return list(cur.fetchall())
    except AttributeError:
        return []


def _fetch_one(conn: Any, sql: str, params: Any = None) -> Any:
    cur = _execute(conn, sql, params)
    if cur is None:
        return None
    try:
        return cur.fetchone()
    except AttributeError:
        return None


@contextlib.contextmanager
def _transaction(conn: Any) -> Iterator[None]:
    """psycopg 连接优先用 transaction()（自动 commit/rollback）；测试替身无此 API 时退化为直执行。"""
    tx = getattr(conn, "transaction", None)
    if callable(tx):
        with tx():
            yield
        return
    yield
    _commit(conn)


def _commit(conn: Any) -> None:
    """显式提交外层隐式事务。

    psycopg 在 autocommit=False 下，任何 `execute`（含 SELECT pg_advisory_lock）都会开启
    隐式事务；此时 `conn.transaction()` 只会建 savepoint，外层事务最终若随连接归还而回滚，
    迁移会「执行成功但没落库」。因此每个迁移之前先把外层事务提交掉。
    autocommit=True 的连接（CLI/测试）没有外层事务，且 psycopg 禁止调用 commit()。
    """
    if getattr(conn, "autocommit", False):
        return
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()


def ensure_schema_version(conn: Any) -> None:
    """建 schema_version（幂等）；空库的第一步。"""
    with _transaction(conn):
        _execute(conn, SCHEMA_VERSION_DDL)


def applied_versions(conn: Any) -> dict[int, str]:
    """返回 {version: checksum}；schema_version 不存在时返回空 dict（空库）。"""
    probe = _fetch_one(conn, "SELECT to_regclass(%s)", ("schema_version",))
    if probe is None or probe[0] is None:
        return {}
    rows = _fetch_all(conn, _APPLIED_SQL)
    out: dict[int, str] = {}
    for row in rows:
        try:
            version, _name, checksum = int(row[0]), row[1], str(row[2])
        except (IndexError, TypeError, ValueError) as exc:
            raise MigrationError(f"malformed schema_version row: {row!r}") from exc
        out[version] = checksum
    return out


def current_version(conn: Any) -> int:
    """数据库当前版本（无记录 = 0）。"""
    return max(applied_versions(conn), default=0)


def pending_migrations(conn: Any, migrations: Iterable[Migration] | None = None) -> list[Migration]:
    """尚未应用的迁移（按版本升序）。"""
    applied = applied_versions(conn)
    return [m for m in _resolve(migrations) if m.version not in applied]


def apply_migrations(
    conn: Any,
    migrations: Iterable[Migration] | None = None,
    *,
    use_advisory_lock: bool = True,
) -> list[int]:
    """应用全部待执行迁移，返回本次实际应用的版本号列表。

    - 先做 drift 检查：已应用版本被改写 / 代码版本落后于库，直接拒绝，绝不「降级覆盖」。
    - 每个迁移一个事务：语句 + schema_version 记录同事务提交。
    """
    resolved = _resolve(migrations)
    _validate_registry(resolved)

    if use_advisory_lock:
        _execute(conn, "SELECT pg_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
    try:
        ensure_schema_version(conn)
        applied = applied_versions(conn)
        _raise_on_drift(applied, resolved)
        # 关键：提交 advisory_lock / schema_version 探测留下的隐式事务，
        # 否则后面的 conn.transaction() 只是 savepoint，最终随连接归还会滚掉全部迁移。
        _commit(conn)

        applied_versions_out: list[int] = []
        for migration in resolved:
            if migration.version in applied:
                continue
            started = time.monotonic()
            with _transaction(conn):
                for statement in migration.statements:
                    _execute(conn, statement)
                duration_ms = int((time.monotonic() - started) * 1000)
                _execute(
                    conn,
                    _INSERT_VERSION_SQL,
                    (migration.version, migration.name, migration.checksum(), duration_ms),
                )
            applied_versions_out.append(migration.version)
        return applied_versions_out
    finally:
        if use_advisory_lock:
            try:
                _execute(conn, "SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
                _commit(conn)
            except Exception:  # noqa: BLE001 - 解锁失败不应掩盖迁移结果，但必须留痕
                import logging

                logging.getLogger(__name__).warning(
                    "db.migrations.advisory_unlock_failed key=%s error_code=db.migrations.advisory_unlock_failed",
                    _ADVISORY_LOCK_KEY,
                    exc_info=True,
                )


def drift_report(conn: Any, migrations: Iterable[Migration] | None = None) -> list[str]:
    """返回 drift 描述列表；空列表 = 一致。调用方（CI/部署）据此决定是否阻断。"""
    resolved = _resolve(migrations)
    _validate_registry(resolved)
    applied = applied_versions(conn)

    problems = _drift_problems(applied, resolved)
    for table in EXPECTED_TABLES:
        try:
            # 不带 schema 前缀：按 search_path 解析，测试可用临时 schema 隔离。
            row = _fetch_one(conn, "SELECT to_regclass(%s)", (table,))
        except Exception as exc:  # noqa: BLE001 - 探测失败本身就是 drift 信号
            problems.append(f"cannot probe table {table}: {type(exc).__name__}")
            continue
        if not row or row[0] is None:
            problems.append(f"missing table: {table}")
    return problems


def status(conn: Any, migrations: Iterable[Migration] | None = None) -> dict[str, Any]:
    """运维状态快照（CLI/`/ready` 可复用）。"""
    resolved = _resolve(migrations)
    applied = applied_versions(conn)
    pending = [m for m in resolved if m.version not in applied]
    return {
        "code_version": max((m.version for m in resolved), default=0),
        "db_version": max(applied, default=0),
        "applied": [{"version": v, "checksum": c} for v, c in sorted(applied.items())],
        "pending": [{"version": m.version, "name": m.name} for m in pending],
        "drift": drift_report(conn, resolved),
    }


def _resolve(migrations: Iterable[Migration] | None) -> tuple[Migration, ...]:
    if migrations is None:
        return load_migrations()
    return tuple(migrations)


def _validate_registry(migrations: tuple[Migration, ...]) -> None:
    if not migrations:
        raise MigrationError("migration registry is empty")
    expected = 1
    for migration in migrations:
        if migration.version != expected:
            raise MigrationError(
                f"migration versions must be contiguous from 1: expected {expected}, got {migration.version}"
            )
        if not migration.statements:
            raise MigrationError(f"migration {migration.version} has no statements")
        expected += 1


def _raise_on_drift(applied: dict[int, str], migrations: tuple[Migration, ...]) -> None:
    problems = _drift_problems(applied, migrations)
    if problems:
        raise MigrationDriftError("; ".join(problems))


def _drift_problems(applied: dict[int, str], migrations: tuple[Migration, ...]) -> list[str]:
    by_version = {m.version: m for m in migrations}
    problems: list[str] = []
    for version, checksum in sorted(applied.items()):
        migration = by_version.get(version)
        if migration is None:
            problems.append(f"database has version {version} which code does not know (database ahead of code)")
            continue
        if migration.checksum() != checksum:
            problems.append(
                f"checksum mismatch for version {version} ({migration.name}): "
                "applied migration was edited; add a new migration instead"
            )
    if applied:
        missing = sorted(set(range(1, max(applied) + 1)) - set(applied))
        if missing:
            problems.append(f"database is missing intermediate versions: {missing}")
    return problems


__all__ = [
    "EXPECTED_TABLES",
    "MigrationDriftError",
    "MigrationError",
    "SCHEMA_VERSION_DDL",
    "apply_migrations",
    "applied_versions",
    "current_version",
    "drift_report",
    "ensure_schema_version",
    "pending_migrations",
    "status",
]

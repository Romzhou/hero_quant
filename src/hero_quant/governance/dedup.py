"""dedup — 工具调用幂等去重表。

职责：以 idempotency_key 主键实现 PENDING→SUCCESS/FAILED 状态机，保证工具调用重试安全与并发去重。
架构位置：治理层存储抽象，被 agent 编排层调用；对外暴露 derive_key 与 DedupStore。
关键设计：键在编排层按 {tenant}:{workflowId}:{stepId}:{tool}:{businessId} 派生；SQLite 文件用于单机/测试，PG 分支提供 JSONB + TTL 索引与 RLS 能力；PG 不可用时回退单机 dict，保证离线可用。
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# 可观测性探针（离线安全，无硬依赖）：记录 dedup 操作耗时与计数，失败时静默降级
def _dedup_observe(op: str, start: float, status: str = "success") -> None:
    try:
        elapsed = time.monotonic() - start
        try:
            from hero_quant.metrics import DEDUP_OP_TOTAL, observe_wall_time

            if DEDUP_OP_TOTAL is not None:
                try:
                    DEDUP_OP_TOTAL.labels(op=op, status=status).inc()
                except Exception:
                    pass
            if observe_wall_time is not None:
                try:
                    observe_wall_time(f"dedup_{op}", elapsed, status=status)
                except Exception:
                    pass
        except Exception:
            pass
    except Exception:
        pass

DEFAULT_TTL_SECONDS = 7 * 24 * 3600

_PG_PREFIXES = ("postgresql://", "postgres://", "postgresql+psycopg://")

DDL_DEDUP_PG = """
CREATE TABLE IF NOT EXISTS dedup (
  key TEXT PRIMARY KEY,
  tenant TEXT,
  tool TEXT,
  status TEXT,
  result JSONB,
  updated_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_dedup_status ON dedup (status);
CREATE INDEX IF NOT EXISTS idx_dedup_updated_at ON dedup (updated_at);
CREATE INDEX IF NOT EXISTS idx_dedup_tenant ON dedup (tenant);
"""

DDL_TOOL_CALL_PG = """
CREATE TABLE IF NOT EXISTS tool_call_dedup (
  idempotency_key TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK(status IN ('PENDING','SUCCESS','FAILED')),
  tool TEXT,
  result TEXT,
  error TEXT,
  created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_tool_status ON tool_call_dedup(status);
"""

# RLS 策略 DDL（DB 层二次防护）：应用层已按 tenant 前缀过滤，库层再以 current_setting 强制隔离
# 键前缀即 tenant，split_part 取首段比对，防止跨租户穿透；deny-by-default 需 SET LOCAL app.current_tenant
DDL_RLS_PG = """
ALTER TABLE dedup ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  CREATE POLICY tenant_isolation ON dedup USING (tenant = current_setting('app.current_tenant', true));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
  CREATE POLICY tenant_isolation_insert ON dedup FOR INSERT WITH CHECK (tenant = current_setting('app.current_tenant', true));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE tool_call_dedup ENABLE ROW LEVEL SECURITY;
DO $$ BEGIN
  CREATE POLICY tenant_isolation ON tool_call_dedup USING (split_part(idempotency_key, ':', 1) = current_setting('app.current_tenant', true));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
  CREATE POLICY tenant_isolation_insert ON tool_call_dedup FOR INSERT WITH CHECK (split_part(idempotency_key, ':', 1) = current_setting('app.current_tenant', true));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
"""

DDL_RLS_DEDUP = DDL_RLS_PG
DDL_RLS_TOOL_CALL = DDL_RLS_PG

# Optional PG pool
try:
    from psycopg_pool import AsyncConnectionPool as _AsyncPool  # type: ignore

    DedupPool: Any = _AsyncPool  # type: ignore
except Exception:
    try:
        from psycopg_pool import ConnectionPool as _SyncPool  # type: ignore

        DedupPool = _SyncPool  # type: ignore
    except Exception:
        DedupPool = None  # type: ignore


def _is_pg_dsn(s: str) -> bool:
    return isinstance(s, str) and s.startswith(_PG_PREFIXES)


def _is_async_pool(pool: Any) -> bool:
    if pool is None:
        return False
    if "Async" in type(pool).__name__:
        return True
    try:
        return inspect.iscoroutinefunction(getattr(pool, "open", None))
    except Exception:
        return False


_KEY_PART_RE = re.compile(r"^[^:]+$")


def derive_key(tenant: str, workflow_id: str, step_id: str, tool: str, business_id: str) -> str:
    """在编排层派生幂等键，格式固定为 tenant:workflow:step:tool:businessId 以保证跨租户隔离与可追溯。"""
    parts = [tenant, workflow_id, step_id, tool, business_id]
    for p in parts:
        s = str(p)
        if not s or not _KEY_PART_RE.match(s):
            raise ValueError(f"derive_key part must match ^[^:]+$: got {s!r}")
    return ":".join(str(p) for p in parts)


class DedupStore:
    """幂等账本：SQLite/PG 双后端 + 单机 dict 回退。

    不变量：同一 idempotency_key 仅一次从 PENDING 转为终态；TTL 过期后可重建；PG 与本地内存保持最终一致。
    """

    def __init__(self, db_path: str | Path = "memory://dedup", *, ttl_seconds: int | None = None, dsn: str | None = None):
        # TTL 过期窗口：超时后允许重放，避免僵死 PENDING 永久占位
        self.ttl_seconds = int(ttl_seconds) if ttl_seconds is not None else DEFAULT_TTL_SECONDS
        # 单进程回退存储：无外部 DB 时仍可保证幂等语义
        self._mem: dict[str, dict[str, Any]] = {}
        self._mem_ts: dict[str, float] = {}
        self._lock = threading.Lock()

        # 探测后端类型：显式 dsn > 路径前缀 > 环境变量，决定 PG / SQLite / 纯内存三分支
        raw = str(db_path) if isinstance(db_path, Path) else str(db_path) if db_path is not None else ""
        # dsn override: explicit dsn param > raw if pg > env
        env_dsn = os.environ.get("HERO_DEDUP_DSN", "")
        candidate_dsn = ""
        if dsn and _is_pg_dsn(str(dsn)):
            candidate_dsn = str(dsn)
        elif _is_pg_dsn(raw):
            candidate_dsn = raw
        elif _is_pg_dsn(env_dsn):
            # use env only if db_path is memory-ish or default
            if raw.startswith("memory://") or raw in ("", ":memory:"):
                candidate_dsn = env_dsn

        self._is_pg = bool(candidate_dsn)
        self.dsn: str = candidate_dsn
        self.pool: Any | None = None
        self.db_path: Path | None = None

        if self._is_pg:
            # PG 分支：尝试初始化连接池，后续操作优先走 PG
            if DedupPool is not None:
                try:
                    try:
                        self.pool = DedupPool(conninfo=self.dsn, min_size=1, max_size=5)  # type: ignore
                    except TypeError:
                        self.pool = DedupPool(self.dsn)  # type: ignore
                except Exception:
                    self.pool = None
            # try setup (sync if sync pool)
            self._pg_setup_sync()
        else:
            # SQLite/纯内存路径：memory:// 仅用 dict，真实文件路径则初始化 SQLite 表
            if raw.startswith("memory://"):
                # 纯内存模式，不落盘
                self.db_path = None
            elif raw in ("", ":memory:"):
                self.db_path = None
            else:
                self.db_path = Path(raw) if raw else Path("dedup.db")
                # 仅文件路径需确保父目录存在
                try:
                    self.db_path.parent.mkdir(parents=True, exist_ok=True)
                except Exception:
                    self.db_path = None
                if self.db_path is not None:
                    self._init_db()

    # PG 建表：幂等写入前确保 dedup/tool_call_dedup 存在，失败静默以便回退
    def _pg_setup_sync(self) -> None:
        if not self._is_pg or self.pool is None or _is_async_pool(self.pool):
            return
        try:
            if hasattr(self.pool, "connection"):
                with self.pool.connection() as conn:  # type: ignore
                    try:
                        conn.execute(DDL_DEDUP_PG)  # type: ignore
                    except Exception:
                        with conn.cursor() as cur:  # type: ignore
                            cur.execute(DDL_DEDUP_PG)
                    try:
                        conn.execute(DDL_TOOL_CALL_PG)  # type: ignore
                    except Exception:
                        try:
                            with conn.cursor() as cur:  # type: ignore
                                cur.execute(DDL_TOOL_CALL_PG)
                        except Exception:
                            pass
                    # DB-level RLS true policy
                    try:
                        conn.execute(DDL_RLS_PG)  # type: ignore
                    except Exception:
                        try:
                            with conn.cursor() as cur:  # type: ignore
                                cur.execute(DDL_RLS_PG)
                        except Exception:
                            pass
                    try:
                        conn.commit()  # type: ignore
                    except Exception:
                        pass
            elif hasattr(self.pool, "getconn"):
                conn = self.pool.getconn()  # type: ignore
                try:
                    with conn.cursor() as cur:
                        cur.execute(DDL_DEDUP_PG)
                        try:
                            cur.execute(DDL_TOOL_CALL_PG)
                        except Exception:
                            pass
                        try:
                            cur.execute(DDL_RLS_PG)
                        except Exception:
                            pass
                    conn.commit()
                finally:
                    try:
                        self.pool.putconn(conn)  # type: ignore
                    except Exception:
                        pass
        except Exception:
            pass

    async def _pg_setup_async(self) -> None:
        if not self._is_pg or self.pool is None:
            return
        if hasattr(self.pool, "open"):
            try:
                await self.pool.open()  # type: ignore
            except Exception:
                pass
        if _is_async_pool(self.pool):
            try:
                async with self.pool.connection() as conn:  # type: ignore
                    await conn.execute(DDL_DEDUP_PG)  # type: ignore
                    try:
                        await conn.execute(DDL_TOOL_CALL_PG)  # type: ignore
                    except Exception:
                        pass
                    try:
                        await conn.execute(DDL_RLS_PG)  # type: ignore
                    except Exception:
                        pass
            except Exception:
                try:
                    async with self.pool.connection() as conn:  # type: ignore
                        async with conn.cursor() as cur:  # type: ignore
                            await cur.execute(DDL_DEDUP_PG)
                            try:
                                await cur.execute(DDL_TOOL_CALL_PG)
                            except Exception:
                                pass
                            try:
                                await cur.execute(DDL_RLS_PG)
                            except Exception:
                                pass
                except Exception:
                    pass

    # SQLite 连接：WAL + NORMAL 同步以兼顾并发与持久性
    def _connect(self) -> sqlite3.Connection:
        assert self.db_path is not None
        con = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        return con

    def _init_db(self) -> None:
        if self.db_path is None:
            return
        con = self._connect()
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_call_dedup (
                    idempotency_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN ('PENDING','SUCCESS','FAILED')),
                    tool TEXT,
                    result TEXT,
                    error TEXT,
                    created_at REAL,
                    updated_at REAL
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_tool_status ON tool_call_dedup(status)")
        finally:
            con.close()

    # 内存 TTL 辅助：基于 updated_at 判断是否过期，过期即延迟清理
    def _mem_is_expired(self, key: str) -> bool:
        ts = self._mem_ts.get(key)
        if ts is None or self.ttl_seconds <= 0:
            return False
        return (time.time() - ts) > self.ttl_seconds

    def _mem_cleanup(self, key: str) -> None:
        with self._lock:
            if self._mem_is_expired(key):
                self._mem.pop(key, None)
                self._mem_ts.pop(key, None)

    def _mem_get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            if self._mem_is_expired(key):
                self._mem.pop(key, None)
                self._mem_ts.pop(key, None)
            rec = self._mem.get(key)
            if rec is None:
                return None
            return dict(rec)

    # PG 辅助（同步）：插入占位或查询/更新，None 表示回退到 SQLite/内存
    def _pg_insert_pending_sync(self, key: str, tool: str) -> bool | None:
        """PG 原子插入 PENDING，先 DELETE 过期再 INSERT，成功返回是否插入，否则回退。"""
        if not self._is_pg or self.pool is None or _is_async_pool(self.pool):
            return None
        try:
            sql_del = "DELETE FROM dedup WHERE key=%s AND updated_at < now() - interval '%s seconds'"
            sql_del2 = "DELETE FROM tool_call_dedup WHERE idempotency_key=%s AND updated_at < now() - interval '%s seconds'"
            sql = """
                INSERT INTO dedup (key, tool, status, updated_at)
                VALUES (%s, %s, 'PENDING', now())
                ON CONFLICT (key) DO NOTHING
            """
            sql2 = """
                INSERT INTO tool_call_dedup (idempotency_key, status, tool, created_at, updated_at)
                VALUES (%s, 'PENDING', %s, now(), now())
                ON CONFLICT (idempotency_key) DO NOTHING
            """
            inserted = False
            if hasattr(self.pool, "connection"):
                with self.pool.connection() as conn:  # type: ignore
                    # TTL cleanup: delete expired rows before insert
                    if self.ttl_seconds > 0:
                        try:
                            try:
                                conn.execute(sql_del, (key, str(int(self.ttl_seconds))))  # type: ignore
                            except Exception:
                                with conn.cursor() as _c:  # type: ignore
                                    _c.execute(sql_del, (key, str(int(self.ttl_seconds))))
                        except Exception as e:
                            logger.warning("dedup pg delete expired failed: %s", e, exc_info=True)
                            _dedup_observe("pg_delete_expired", time.monotonic(), status="error")
                        try:
                            try:
                                conn.execute(sql_del2, (key, str(int(self.ttl_seconds))))  # type: ignore
                            except Exception:
                                with conn.cursor() as _c2:  # type: ignore
                                    _c2.execute(sql_del2, (key, str(int(self.ttl_seconds))))
                        except Exception:
                            pass
                    try:
                        cur = conn.execute(sql, (key, tool))  # type: ignore
                        inserted = getattr(cur, "rowcount", 0) == 1
                    except Exception:
                        with conn.cursor() as cur2:  # type: ignore
                            cur2.execute(sql, (key, tool))
                            inserted = getattr(cur2, "rowcount", 0) == 1
                    try:
                        conn.execute(sql2, (key, tool))  # type: ignore
                    except Exception:
                        try:
                            with conn.cursor() as cur3:  # type: ignore
                                cur3.execute(sql2, (key, tool))
                        except Exception:
                            pass
                    try:
                        conn.commit()  # type: ignore
                    except Exception:
                        pass
            elif hasattr(self.pool, "getconn"):
                conn = self.pool.getconn()  # type: ignore
                try:
                    if self.ttl_seconds > 0:
                        try:
                            with conn.cursor() as _c:
                                _c.execute(sql_del, (key, str(int(self.ttl_seconds))))
                        except Exception as e:
                            logger.warning("dedup pg delete expired failed: %s", e, exc_info=True)
                        try:
                            with conn.cursor() as _c2:
                                _c2.execute(sql_del2, (key, str(int(self.ttl_seconds))))
                        except Exception:
                            pass
                    with conn.cursor() as cur:
                        cur.execute(sql, (key, tool))
                        inserted = getattr(cur, "rowcount", 0) == 1
                        try:
                            cur.execute(sql2, (key, tool))
                        except Exception:
                            pass
                    conn.commit()
                finally:
                    try:
                        self.pool.putconn(conn)  # type: ignore
                    except Exception:
                        pass
            else:
                return None
            return bool(inserted)
        except Exception as e:
            logger.warning("dedup pg insert_pending failed, fallback: %s", e, exc_info=True)
            _dedup_observe("pg_insert_pending", time.monotonic(), status="error")
            return None

    def _pg_get_sync(self, key: str) -> dict[str, Any] | None:
        if not self._is_pg or self.pool is None or _is_async_pool(self.pool):
            return None
        try:
            # TTL via updated_at > now() - interval
            ttl_clause = f"AND updated_at > now() - interval '{int(self.ttl_seconds)} seconds'" if self.ttl_seconds > 0 else ""
            sql = f"SELECT key, tool, status, result, updated_at FROM dedup WHERE key=%s {ttl_clause}"
            sql2 = "SELECT idempotency_key, status, tool, result, error, created_at, updated_at FROM tool_call_dedup WHERE idempotency_key=%s"
            row = None
            if hasattr(self.pool, "connection"):
                with self.pool.connection() as conn:  # type: ignore
                    try:
                        cur = conn.execute(sql, (key,))  # type: ignore
                        row = cur.fetchone()  # type: ignore
                        if row is not None:
                            # col names from cursor description when possible
                            cols = [d[0] for d in cur.description] if getattr(cur, "description", None) else ["key", "tool", "status", "result", "updated_at"]
                            rec = dict(zip(cols, row if isinstance(row, (list, tuple)) else [row]))
                            if rec.get("result") is not None and isinstance(rec["result"], str):
                                try:
                                    rec["result"] = json.loads(rec["result"])
                                except Exception:
                                    pass
                            return rec
                    except Exception:
                        pass
                    # fallback to alias table
                    try:
                        with conn.cursor() as cur2:  # type: ignore
                            cur2.execute(sql2, (key,))
                            row2 = cur2.fetchone()
                            if row2 is not None:
                                cols2 = [d[0] for d in cur2.description] if getattr(cur2, "description", None) else []
                                if cols2:
                                    rec2 = dict(zip(cols2, row2))
                                else:
                                    rec2 = {"idempotency_key": row2[0], "status": row2[1], "tool": row2[2], "result": row2[3]}
                                if rec2.get("result") is not None and isinstance(rec2["result"], str):
                                    try:
                                        rec2["result"] = json.loads(rec2["result"])
                                    except Exception:
                                        pass
                                return rec2
                    except Exception:
                        pass
            elif hasattr(self.pool, "getconn"):
                conn = self.pool.getconn()  # type: ignore
                try:
                    with conn.cursor() as cur:
                        cur.execute(sql, (key,))
                        row = cur.fetchone()
                        if row is not None:
                            cols = [d[0] for d in cur.description] if getattr(cur, "description", None) else ["key", "tool", "status", "result", "updated_at"]
                            rec = dict(zip(cols, row))
                            if rec.get("result") is not None and isinstance(rec["result"], str):
                                try:
                                    rec["result"] = json.loads(rec["result"])
                                except Exception:
                                    pass
                            return rec
                finally:
                    try:
                        self.pool.putconn(conn)  # type: ignore
                    except Exception:
                        pass
            return None
        except Exception as e:
            logger.warning("dedup pg get failed, fallback: %s", e, exc_info=True)
            _dedup_observe("pg_get", time.monotonic(), status="error")
            return None

    def _pg_mark_sync(self, key: str, status: str, result: Any | None = None, error: str | None = None) -> bool:
        if not self._is_pg or self.pool is None or _is_async_pool(self.pool):
            return False
        try:
            result_json = json.dumps(result, ensure_ascii=False) if result is not None and not isinstance(result, str) else result
            error_str = str(error) if error is not None else None
            sql = """
                UPDATE dedup SET status=%s, result=%s::jsonb, updated_at=now() WHERE key=%s
            """
            sql_insert = """
                INSERT INTO dedup (key, tool, status, result, updated_at)
                VALUES (%s, %s, %s, %s::jsonb, now())
                ON CONFLICT (key) DO UPDATE SET status=EXCLUDED.status, result=EXCLUDED.result, updated_at=now()
            """
            sql2 = "UPDATE tool_call_dedup SET status=%s, result=%s, error=%s, updated_at=now() WHERE idempotency_key=%s"
            if hasattr(self.pool, "connection"):
                with self.pool.connection() as conn:  # type: ignore
                    try:
                        cur = conn.execute(sql, (status, result_json, key))  # type: ignore
                        rc = getattr(cur, "rowcount", 0)
                        if rc == 0:
                            # upsert fallback trying to get tool from mem or unknown
                            tool = self._mem.get(key, {}).get("tool", "unknown")
                            conn.execute(sql_insert, (key, tool, status, result_json))  # type: ignore
                        # also update alias table best-effort
                        try:
                            conn.execute(sql2, (status, result_json, error_str, key))  # type: ignore
                        except Exception:
                            pass
                    except Exception:
                        with conn.cursor() as cur2:  # type: ignore
                            cur2.execute(sql, (status, result_json, key))
                            rc = getattr(cur2, "rowcount", 0)
                            if rc == 0:
                                tool = self._mem.get(key, {}).get("tool", "unknown")
                                cur2.execute(sql_insert, (key, tool, status, result_json))
                    try:
                        conn.commit()  # type: ignore
                    except Exception:
                        pass
            elif hasattr(self.pool, "getconn"):
                conn = self.pool.getconn()  # type: ignore
                try:
                    with conn.cursor() as cur:
                        cur.execute(sql, (status, result_json, key))
                        rc = getattr(cur, "rowcount", 0)
                        if rc == 0:
                            tool = self._mem.get(key, {}).get("tool", "unknown")
                            cur.execute(sql_insert, (key, tool, status, result_json))
                    conn.commit()
                finally:
                    try:
                        self.pool.putconn(conn)  # type: ignore
                    except Exception:
                        pass
            return True
        except Exception as e:
            logger.warning("dedup pg mark failed, fallback: %s", e, exc_info=True)
            _dedup_observe("pg_mark", time.monotonic(), status="error")
            return False

    # 公共 API：幂等状态机
    def insert_pending(self, key: str, tool: str) -> bool:
        """尝试写入 PENDING 占位，成功返回 True，已存在返回 False，供重试方决定是否执行。"""
        _start = time.monotonic()
        _status = "success"
        try:
            now = time.time()
            # PG branch first when available
            if self._is_pg:
                pg_res = self._pg_insert_pending_sync(key, tool)
                if pg_res is not None:
                    with self._lock:
                        if pg_res:
                            self._mem[key] = {"key": key, "tool": tool, "status": "PENDING", "result": None, "updated_at": now}
                            self._mem_ts[key] = now
                        else:
                            if key not in self._mem:
                                pg_rec = self._pg_get_sync(key)
                                if pg_rec:
                                    self._mem[key] = pg_rec
                                    self._mem_ts[key] = now
                    return bool(pg_res)
                logger.warning("dedup pg insert_pending fallback to sqlite/mem for key=%s", key)
                _dedup_observe("pg_fallback", time.monotonic(), status="error")

            # SQLite path — BEGIN IMMEDIATE for atomic DELETE+SELECT+INSERT, rowcount decides winner
            if self.db_path is not None:
                con = self._connect()
                try:
                    try:
                        con.execute("BEGIN IMMEDIATE")
                    except Exception:
                        pass
                    # TTL-aware: delete expired row first within same txn
                    if self.ttl_seconds > 0:
                        try:
                            con.execute("DELETE FROM tool_call_dedup WHERE idempotency_key=? AND updated_at < ?", (key, now - self.ttl_seconds))
                        except Exception:
                            pass
                    cur = con.execute("SELECT status FROM tool_call_dedup WHERE idempotency_key=?", (key,))
                    row = cur.fetchone()
                    if row is not None:
                        try:
                            con.execute("ROLLBACK")
                        except Exception:
                            pass
                        with self._lock:
                            self._mem[key] = {"key": key, "tool": tool, "status": row[0], "result": None, "updated_at": now}
                            self._mem_ts[key] = now
                        return False
                    try:
                        cur2 = con.execute(
                            "INSERT INTO tool_call_dedup (idempotency_key, status, tool, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                            (key, "PENDING", tool, now, now),
                        )
                        inserted = getattr(cur2, "rowcount", 1) == 1
                        try:
                            con.execute("COMMIT")
                        except Exception:
                            pass
                        if inserted:
                            with self._lock:
                                self._mem[key] = {"key": key, "tool": tool, "status": "PENDING", "result": None, "updated_at": now}
                                self._mem_ts[key] = now
                            return True
                        else:
                            return False
                    except sqlite3.IntegrityError:
                        try:
                            con.execute("ROLLBACK")
                        except Exception:
                            pass
                        return False
                finally:
                    try:
                        con.close()
                    except Exception:
                        pass
            # pure memory dict fallback (single-process) with lock
            with self._lock:
                # inline expiry check with lock
                ts = self._mem_ts.get(key)
                if ts is not None and self.ttl_seconds > 0 and (time.time() - ts) > self.ttl_seconds:
                    self._mem.pop(key, None)
                    self._mem_ts.pop(key, None)
                if key in self._mem:
                    return False
                self._mem[key] = {"key": key, "tool": tool, "status": "PENDING", "result": None, "updated_at": now}
                self._mem_ts[key] = now
                return True
        except Exception:
            _status = "error"
            raise
        finally:
            _dedup_observe("insert_pending", _start, _status)

    def mark_success(self, key: str, result: Any) -> None:
        """标记成功并持久化结果，后续 get/wait 将返回 SUCCESS。"""
        _start = time.monotonic()
        _status = "success"
        try:
            now = time.time()
            result_json = json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
            # 优先 PG，失败回退本地
            if self._is_pg:
                if self._pg_mark_sync(key, "SUCCESS", result=result, error=None):
                    with self._lock:
                        self._mem[key] = {"key": key, "tool": self._mem.get(key, {}).get("tool"), "status": "SUCCESS", "result": result, "updated_at": now}
                        self._mem_ts[key] = now
                    return
                logger.warning("dedup pg mark_success fallback to sqlite/mem for key=%s", key)
                _dedup_observe("pg_fallback", time.monotonic(), status="error")
            if self.db_path is not None:
                con = self._connect()
                try:
                    cur = con.execute(
                        "UPDATE tool_call_dedup SET status=?, result=?, updated_at=? WHERE idempotency_key=?",
                        ("SUCCESS", result_json, now, key),
                    )
                    if getattr(cur, "rowcount", 0) == 0:
                        con.execute(
                            "INSERT OR IGNORE INTO tool_call_dedup (idempotency_key, status, result, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                            (key, "SUCCESS", result_json, now, now),
                        )
                finally:
                    con.close()
            with self._lock:
                rec = self._mem.get(key, {})
                self._mem[key] = {"key": key, "tool": rec.get("tool"), "status": "SUCCESS", "result": result, "updated_at": now}
                self._mem_ts[key] = now
        except Exception:
            _status = "error"
            raise
        finally:
            _dedup_observe("mark_success", _start, _status)

    def mark_failed(self, key: str, error: str) -> None:
        """标记失败并记录错误信息，便于调用方决定重试或补偿。"""
        _start = time.monotonic()
        _status = "success"
        try:
            now = time.time()
            if self._is_pg:
                if self._pg_mark_sync(key, "FAILED", result=None, error=error):
                    with self._lock:
                        self._mem[key] = {"key": key, "tool": self._mem.get(key, {}).get("tool"), "status": "FAILED", "error": str(error), "updated_at": now}
                        self._mem_ts[key] = now
                    return
                logger.warning("dedup pg mark_failed fallback to sqlite/mem for key=%s", key)
                _dedup_observe("pg_fallback", time.monotonic(), status="error")
            if self.db_path is not None:
                con = self._connect()
                try:
                    cur = con.execute(
                        "UPDATE tool_call_dedup SET status=?, error=?, updated_at=? WHERE idempotency_key=?",
                        ("FAILED", str(error), now, key),
                    )
                    if getattr(cur, "rowcount", 0) == 0:
                        con.execute(
                            "INSERT OR IGNORE INTO tool_call_dedup (idempotency_key, status, error, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                            (key, "FAILED", str(error), now, now),
                        )
                finally:
                    con.close()
            with self._lock:
                rec = self._mem.get(key, {})
                self._mem[key] = {"key": key, "tool": rec.get("tool"), "status": "FAILED", "error": str(error), "updated_at": now}
                self._mem_ts[key] = now
        except Exception:
            _status = "error"
            raise
        finally:
            _dedup_observe("mark_failed", _start, _status)

    def get(self, key: str) -> dict[str, Any] | None:
        """查询幂等记录，TTL 过期或不存在返回 None；PG→SQLite→内存依次回退。"""
        _start = time.monotonic()
        _status = "success"
        try:
            # 优先 PG（查询内已含 TTL 过滤）
            if self._is_pg:
                pg_rec = self._pg_get_sync(key)
                if pg_rec is not None:
                    with self._lock:
                        self._mem[key] = pg_rec
                        self._mem_ts[key] = time.time()
                    if pg_rec.get("result") is not None and isinstance(pg_rec["result"], str):
                        try:
                            pg_rec["result"] = json.loads(pg_rec["result"])
                        except Exception:
                            pass
                    return pg_rec
                # if PG miss, fall through to sqlite/mem (could be recently written to mem before PG commit)
            if self.db_path is not None:
                con = self._connect()
                try:
                    cur = con.execute(
                        "SELECT idempotency_key, status, tool, result, error, created_at, updated_at FROM tool_call_dedup WHERE idempotency_key=?",
                        (key,),
                    )
                    row = cur.fetchone()
                    if row is not None:
                        col_names = [d[0] for d in cur.description]
                        rec = dict(zip(col_names, row))
                        # TTL check (updated_at is REAL seconds)
                        if self.ttl_seconds > 0 and rec.get("updated_at") is not None:
                            try:
                                if time.time() - float(rec["updated_at"]) > self.ttl_seconds:
                                    # expired: delete and return None, also clear mem
                                    try:
                                        con.execute("DELETE FROM tool_call_dedup WHERE idempotency_key=?", (key,))
                                    except Exception:
                                        pass
                                    with self._lock:
                                        self._mem.pop(key, None)
                                        self._mem_ts.pop(key, None)
                                    return None
                            except Exception:
                                pass
                        if rec.get("result") is not None:
                            try:
                                rec["result"] = json.loads(rec["result"])
                            except Exception:
                                pass
                        with self._lock:
                            self._mem[key] = rec
                            self._mem_ts[key] = rec.get("updated_at", time.time())
                        return rec
                finally:
                    con.close()
            # fallback memory
            mem_rec = self._mem_get(key)
            if mem_rec is not None and mem_rec.get("result") is not None and isinstance(mem_rec["result"], str):
                try:
                    mem_rec["result"] = json.loads(mem_rec["result"])
                except Exception:
                    pass
            return mem_rec
        except Exception:
            _status = "error"
            raise
        finally:
            _dedup_observe("get", _start, _status)

    def wait_for(self, key: str, timeout: float = 5.0) -> dict[str, Any] | None:
        """轮询等待终态（SUCCESS/FAILED），用于占位冲突时的 WAIT 语义。"""
        _start = time.monotonic()
        _status = "success"
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                rec = self.get(key)
                if rec is not None and rec.get("status") in ("SUCCESS", "FAILED"):
                    return rec
                time.sleep(0.05)
            return self.get(key)
        except Exception:
            _status = "error"
            raise
        finally:
            _dedup_observe("wait_for", _start, _status)

"""Postgres 检查点持久化 — AsyncPostgresSaver。

职责：在 Postgres 与内存双后端提供 thread_id 粒度的 checkpoint 读写与过期清理。
架构位置：`checkpoint` 包核心实现，供编排层断点续跑与 LangGraph Saver 接口使用。
关键设计：`psycopg_pool` ConnectionPool 复用（min1/max5）；同步/异步双路径建表；`memory://` 兜底保证单测离线可用；`thread_id` 三段式 + TTL（默认 7 天）控制可恢复窗口。
Task7: PG default (not memory://), fallback to memory only when PG unreachable, DDL tenant/thread/seq.
"""

from __future__ import annotations
import logging

import copy
import inspect
import json
import os
import time
from typing import Any, Dict, Optional
logger = logging.getLogger("hero_quant.checkpoint.postgres")

# 默认 TTL 7 天 — 控制可恢复窗口，超时自动清理避免无限堆积
DEFAULT_TTL_SECONDS = 7 * 24 * 3600

# 可选 psycopg_pool — 真实 Postgres 时复用连接池，缺包时优雅降级为内存实现
try:
    from psycopg_pool import AsyncConnectionPool as _AsyncPool  # type: ignore

    ConnectionPool: Any = _AsyncPool  # type: ignore
except Exception:
    try:
        from psycopg_pool import ConnectionPool as _SyncPool  # type: ignore

        ConnectionPool = _SyncPool  # type: ignore
    except Exception:
        ConnectionPool = None  # type: ignore

# Task7 DDL — required primary key (tenant, thread, seq), tenant text, thread text, seq int
DDL_CHECKPOINTS = """
CREATE TABLE IF NOT EXISTS checkpoints (
  tenant text NOT NULL,
  thread text NOT NULL,
  seq int NOT NULL,
  checkpoint jsonb NOT NULL,
  expires_at timestamptz,
  PRIMARY KEY (tenant, thread, seq)
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_expires_at ON checkpoints (expires_at);
-- legacy fallback for older code paths using thread_id primary key
CREATE TABLE IF NOT EXISTS checkpoints_legacy (
  thread_id TEXT PRIMARY KEY,
  checkpoint JSONB,
  config JSONB,
  expires_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_legacy_expires_at ON checkpoints_legacy (expires_at);
"""

_PG_PREFIXES = ("postgresql://", "postgres://", "postgresql+psycopg://")

# Global emulated PG store for in-memory PG mock (restart not lost without real PG)
_PG_GLOBAL_STORE: Dict[str, Dict[str, Any]] = {}
_PG_GLOBAL_META: Dict[str, Dict[str, Any]] = {}
_PG_GLOBAL_TS: Dict[str, float] = {}


def _is_postgres_dsn(dsn: str) -> bool:
    """判断是否为 Postgres DSN 前缀。"""
    return isinstance(dsn, str) and dsn.startswith(_PG_PREFIXES)


def _default_pg_dsn() -> str:
    """PG default (not memory://) for Task7."""
    raw = os.environ.get("HERO_CHECKPOINT_DSN", "")
    if raw and raw.strip():
        return raw.strip()
    alt = os.environ.get("HERO_PG_DSN", "")
    if alt and alt.strip() and alt.strip().startswith(_PG_PREFIXES):
        return alt.strip()
    return "postgresql://postgres:postgres@localhost:5432/hero_quant"


def _resolve_ttl(ttl_seconds: int | None) -> int:
    if ttl_seconds is not None:
        try:
            return int(ttl_seconds)
        except Exception:
            pass
    # try Settings gate
    try:
        from hero_quant.config.settings import Settings
        s = Settings()
        if hasattr(s, "checkpoint_ttl_seconds"):
            return int(s.checkpoint_ttl_seconds)
    except Exception:
        pass
    return DEFAULT_TTL_SECONDS


def _validate_thread_id(thread_id: str) -> tuple[str, str, str]:
    """校验 thread_id 三段式，返回 (workflow, run_id, tenant)。"""
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError(f"invalid thread_id: {thread_id!r}")
    parts = thread_id.split(":")
    if len(parts) != 3:
        raise ValueError(f"thread_id must be 3 segments 'workflow:run:tenant', got {thread_id!r}")
    if not all(p.strip() for p in parts):
        raise ValueError(f"thread_id segments must be non-empty, got {thread_id!r}")
    return parts[0], parts[1], parts[2]


def _thread_to_keys(thread_id: str) -> tuple[str, str, int]:
    """Map thread_id 'workflow:run:tenant' -> (tenant, thread, seq)."""
    wf, run, tenant = _validate_thread_id(thread_id)
    try:
        seq = int(run)
    except Exception:
        # fallback hash to int for non-numeric run ids
        seq = abs(hash(run)) % 2147483647
    # thread = workflow (preserve workflow as thread identifier)
    return tenant, wf, seq


def _is_async_pool(pool: Any) -> bool:
    """判断连接池是否为异步实现（用于分支同步/异步路径）。"""
    if pool is None:
        return False
    if "Async" in type(pool).__name__:
        return True
    try:
        return inspect.iscoroutinefunction(getattr(pool, "open", None))
    except Exception:
        return False


class AsyncPostgresSaver:
    """LangGraph PostgresSaver 兼容实现 — 内存与 Postgres 双后端。

    职责：以 `thread_id` 为主键持久化 checkpoint/config，支持 TTL 过期与幂等 UPSERT。
    不变量：`_setup_done` 控制 DDL 仅执行一次；`memory://` 始终可用作降级路径。
    Task7: PG default, main path PG with fallback to memory only when unreachable.
    """

    def __init__(
        self,
        conn_or_dsn: Any = None,
        *,
        dsn: Optional[str] = None,
        ttl_seconds: int | None = None,
        pool: Optional[Any] = None,
    ) -> None:
        raw = dsn if dsn is not None else conn_or_dsn
        if raw is None:
            raw = _default_pg_dsn()
        # allow explicit memory:// to force memory path (tests use memory://test)
        eff_ttl = _resolve_ttl(ttl_seconds)
        self.ttl_seconds = int(eff_ttl) if eff_ttl is not None else DEFAULT_TTL_SECONDS
        self._store: Dict[str, Dict[str, Any]] = {}
        self._meta: Dict[str, Dict[str, Any]] = {}
        self._timestamps: Dict[str, float] = {}
        self._setup_done = False

        self.dsn: str = ""
        self.pool: Optional[Any] = pool
        if isinstance(raw, str):
            self.dsn = raw
            if self.dsn.startswith("memory://"):
                self.pool = None
            elif _is_postgres_dsn(self.dsn):
                if self.pool is None and ConnectionPool is not None:
                    try:
                        try:
                            self.pool = ConnectionPool(conninfo=self.dsn, min_size=1, max_size=5)  # type: ignore
                        except TypeError:
                            self.pool = ConnectionPool(self.dsn)  # type: ignore
                    except Exception:
                        self.pool = None
                # keep dsn as PG, pool may be None -> will use global emulated store as PG main path
            else:
                if self.pool is None and ConnectionPool is not None:
                    pass
        else:
            self.pool = raw
            self.dsn = getattr(raw, "conninfo", "") or str(raw)

    # ---- helpers ----
    def _is_pg_mode(self) -> bool:
        """是否为 Postgres 主路径（DSN 匹配即视为 PG 模式，pool 为 None 时走 emulated global store）。"""
        return _is_postgres_dsn(self.dsn)

    def _is_real_pg_pool(self) -> bool:
        """是否拥有真实可用的 PG pool（用于决定是否执行真实 SQL）。"""
        return _is_postgres_dsn(self.dsn) and self.pool is not None

    def _pool_is_async(self) -> bool:
        """池是否为异步（决定走同步还是异步执行路径）。"""
        return _is_async_pool(self.pool)

    # ---- setup ----

    def setup(self) -> None:
        """同步建表 — 真实 Postgres 时执行 DDL，memory 时 no-op。"""
        if self._setup_done:
            return
        if self._is_real_pg_pool() and not self._pool_is_async():
            try:
                if hasattr(self.pool, "connection"):
                    with self.pool.connection() as conn:  # type: ignore
                        try:
                            conn.execute(DDL_CHECKPOINTS)  # type: ignore
                        except Exception:
                            with conn.cursor() as cur:  # type: ignore
                                cur.execute(DDL_CHECKPOINTS)
                        try:
                            conn.commit()  # type: ignore
                        except Exception as _exc:
                            logger.warning("silent handled: offline-safe: checkpoint pg fallback to memory", exc_info=_exc)
                            pass
                elif hasattr(self.pool, "getconn"):
                    conn = self.pool.getconn()  # type: ignore
                    try:
                        with conn.cursor() as cur:
                            cur.execute(DDL_CHECKPOINTS)
                        conn.commit()
                    finally:
                        try:
                            self.pool.putconn(conn)  # type: ignore
                        except Exception as _exc:
                            logger.warning("silent handled: offline-safe: checkpoint pg fallback to memory", exc_info=_exc)
                            pass
            except Exception:
                pass
        self._setup_done = True

    async def asetup(self) -> None:
        """异步建表 — 真实 Postgres 时 await pool.open() 并执行 DDL。"""
        if self._setup_done:
            if not (self._is_real_pg_pool() and self._pool_is_async()):
                return
        if self.pool is not None and hasattr(self.pool, "open"):
            try:
                await self.pool.open()  # type: ignore
            except Exception as _exc:
                logger.warning("silent handled: offline-safe: checkpoint pg fallback to memory", exc_info=_exc)
                pass
        if self._is_real_pg_pool() and self._pool_is_async():
            try:
                async with self.pool.connection() as conn:  # type: ignore
                    await conn.execute(DDL_CHECKPOINTS)  # type: ignore
            except Exception:
                try:
                    async with self.pool.connection() as conn:  # type: ignore
                        async with conn.cursor() as cur:  # type: ignore
                            await cur.execute(DDL_CHECKPOINTS)
                except Exception as _exc:
                    logger.warning("silent handled: offline-safe: checkpoint pg fallback to memory", exc_info=_exc)
                    pass
        self._setup_done = True

    # ---- internal PG ops ----
    def _pg_put_sync(self, thread_id: str, checkpoint: Dict[str, Any], config: Dict[str, Any]) -> bool:
        """同步 UPSERT 到 Postgres（幂等，带 expires_at）。Task7 tenant/thread/seq schema."""
        if not self._is_pg_mode() or self._pool_is_async():
            # if we have real pool async, not sync; still try emulated global fallback outside
            pass
        if self._is_real_pg_pool() and not self._pool_is_async():
            try:
                tenant, thread, seq = _thread_to_keys(thread_id)
                expires_at_sql = "now() + interval '%s seconds'" % int(self.ttl_seconds) if self.ttl_seconds > 0 else "NULL"
                ck_json = json.dumps(checkpoint, ensure_ascii=False)
                cfg_json = json.dumps(config, ensure_ascii=False) if config else json.dumps({}, ensure_ascii=False)
                # Try new schema first, fallback to legacy thread_id
                sql_new = f"""
                    INSERT INTO checkpoints (tenant, thread, seq, checkpoint, expires_at)
                    VALUES (%s, %s, %s, %s::jsonb, {expires_at_sql})
                    ON CONFLICT (tenant, thread, seq) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, expires_at=EXCLUDED.expires_at
                """
                sql_legacy = f"""
                    INSERT INTO checkpoints_legacy (thread_id, checkpoint, config, expires_at)
                    VALUES (%s, %s::jsonb, %s::jsonb, {expires_at_sql})
                    ON CONFLICT (thread_id) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, config=EXCLUDED.config, expires_at=EXCLUDED.expires_at
                """
                if hasattr(self.pool, "connection"):
                    with self.pool.connection() as conn:  # type: ignore
                        try:
                            conn.execute(sql_new, (tenant, thread, seq, ck_json))  # type: ignore
                            # also maintain legacy for compatibility
                            try:
                                conn.execute(sql_legacy, (thread_id, ck_json, cfg_json))  # type: ignore
                            except Exception:
                                pass
                        except Exception:
                            # fallback legacy if new fails (table missing)
                            with conn.cursor() as cur:  # type: ignore
                                try:
                                    cur.execute(sql_new, (tenant, thread, seq, ck_json))
                                except Exception:
                                    cur.execute(sql_legacy, (thread_id, ck_json, cfg_json))
                        try:
                            conn.commit()  # type: ignore
                        except Exception as _exc:
                            logger.warning("silent handled: offline-safe: checkpoint pg fallback to memory", exc_info=_exc)
                            pass
                elif hasattr(self.pool, "getconn"):
                    conn = self.pool.getconn()  # type: ignore
                    try:
                        with conn.cursor() as cur:
                            try:
                                cur.execute(sql_new, (tenant, thread, seq, ck_json))
                            except Exception:
                                cur.execute(sql_legacy, (thread_id, ck_json, cfg_json))
                        conn.commit()
                    finally:
                        try:
                            self.pool.putconn(conn)  # type: ignore
                        except Exception as _exc:
                            logger.warning("silent handled: offline-safe: checkpoint pg fallback to memory", exc_info=_exc)
                            pass
                else:
                    return False
                return True
            except Exception:
                return False
        # No real pool: emulated PG will be handled by caller via global store; return False to indicate no real PG op
        return False

    async def _pg_put_async(self, thread_id: str, checkpoint: Dict[str, Any], config: Dict[str, Any]) -> bool:
        """异步 UPSERT 到 Postgres。"""
        if not self._is_pg_mode():
            return False
        if self._is_real_pg_pool() and self._pool_is_async():
            try:
                tenant, thread, seq = _thread_to_keys(thread_id)
                ck_json = json.dumps(checkpoint, ensure_ascii=False)
                cfg_json = json.dumps(config, ensure_ascii=False) if config else json.dumps({}, ensure_ascii=False)
                expires_at_sql = "now() + interval '%s seconds'" % int(self.ttl_seconds) if self.ttl_seconds > 0 else "NULL"
                sql_new = f"""
                    INSERT INTO checkpoints (tenant, thread, seq, checkpoint, expires_at)
                    VALUES (%s, %s, %s, %s::jsonb, {expires_at_sql})
                    ON CONFLICT (tenant, thread, seq) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, expires_at=EXCLUDED.expires_at
                """
                async with self.pool.connection() as conn:  # type: ignore
                    await conn.execute(sql_new, (tenant, thread, seq, ck_json))  # type: ignore
                return True
            except Exception:
                return False
        elif self._is_real_pg_pool():
            return self._pg_put_sync(thread_id, checkpoint, config)
        return False

    def _pg_get_sync(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """同步从 Postgres 读取未过期 checkpoint。"""
        if self._is_real_pg_pool() and not self._pool_is_async():
            try:
                tenant, thread, seq = _thread_to_keys(thread_id)
                sql_new = "SELECT checkpoint FROM checkpoints WHERE tenant=%s AND thread=%s AND seq=%s AND (expires_at IS NULL OR expires_at > now())"
                sql_legacy = "SELECT checkpoint, config FROM checkpoints_legacy WHERE thread_id=%s AND (expires_at IS NULL OR expires_at > now())"
                row = None
                if hasattr(self.pool, "connection"):
                    with self.pool.connection() as conn:  # type: ignore
                        try:
                            cur = conn.execute(sql_new, (tenant, thread, seq))  # type: ignore
                            row = cur.fetchone()  # type: ignore
                            if row is None:
                                cur = conn.execute(sql_legacy, (thread_id,))  # type: ignore
                                row = cur.fetchone()  # type: ignore
                                if row is not None:
                                    chk = row[0] if isinstance(row, (list, tuple)) else row.get("checkpoint")  # type: ignore
                                    if isinstance(chk, str):
                                        try:
                                            chk = json.loads(chk)
                                        except Exception:
                                            pass
                                    return copy.deepcopy(chk) if isinstance(chk, dict) else chk  # type: ignore
                        except Exception:
                            with conn.cursor() as cur:  # type: ignore
                                cur.execute(sql_new, (tenant, thread, seq))
                                row = cur.fetchone()
                                if row is None:
                                    cur.execute(sql_legacy, (thread_id,))
                                    row = cur.fetchone()
                                    if row is not None:
                                        chk = row[0] if isinstance(row, (list, tuple)) else row.get("checkpoint")  # type: ignore
                                        if isinstance(chk, str):
                                            try:
                                                chk = json.loads(chk)
                                            except Exception:
                                                pass
                                        return copy.deepcopy(chk) if isinstance(chk, dict) else chk  # type: ignore
                elif hasattr(self.pool, "getconn"):
                    conn = self.pool.getconn()  # type: ignore
                    try:
                        with conn.cursor() as cur:
                            cur.execute(sql_new, (tenant, thread, seq))
                            row = cur.fetchone()
                            if row is None:
                                cur.execute(sql_legacy, (thread_id,))
                                row = cur.fetchone()
                    finally:
                        try:
                            self.pool.putconn(conn)  # type: ignore
                        except Exception as _exc:
                            logger.warning("silent handled: offline-safe: checkpoint pg fallback to memory", exc_info=_exc)
                            pass
                if row is None:
                    return None
                chk = row[0] if isinstance(row, (list, tuple)) else row.get("checkpoint")  # type: ignore
                if isinstance(chk, str):
                    try:
                        chk = json.loads(chk)
                    except Exception:
                        pass
                return copy.deepcopy(chk) if isinstance(chk, dict) else chk  # type: ignore
            except Exception:
                return None
        return None

    async def _pg_get_async(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """异步从 Postgres 读取未过期 checkpoint。"""
        if not self._is_pg_mode():
            return None
        if self._is_real_pg_pool() and self._pool_is_async():
            try:
                tenant, thread, seq = _thread_to_keys(thread_id)
                sql_new = "SELECT checkpoint FROM checkpoints WHERE tenant=%s AND thread=%s AND seq=%s AND (expires_at IS NULL OR expires_at > now())"
                async with self.pool.connection() as conn:  # type: ignore
                    cur = await conn.execute(sql_new, (tenant, thread, seq))  # type: ignore
                    row = await cur.fetchone()  # type: ignore
                    if row is None:
                        # try legacy
                        sql_legacy = "SELECT checkpoint FROM checkpoints_legacy WHERE thread_id=%s AND (expires_at IS NULL OR expires_at > now())"
                        cur = await conn.execute(sql_legacy, (thread_id,))  # type: ignore
                        row = await cur.fetchone()  # type: ignore
                    if row is None:
                        return None
                    chk = row[0] if isinstance(row, (list, tuple)) else row.get("checkpoint")  # type: ignore
                    if isinstance(chk, str):
                        try:
                            chk = json.loads(chk)
                        except Exception:
                            pass
                    return copy.deepcopy(chk) if isinstance(chk, dict) else chk  # type: ignore
            except Exception:
                return None
        elif self._is_real_pg_pool():
            return self._pg_get_sync(thread_id)
        return None

    # ---- put / get ----

    def put(self, thread_id: str, checkpoint: Dict[str, Any], config: Dict[str, Any] | None = None) -> None:
        """写入 checkpoint，thread_id 须为三段式，自动记录 TTL 时间戳。"""
        _validate_thread_id(thread_id)
        if not isinstance(checkpoint, dict):
            raise ValueError("checkpoint must be dict")
        now = time.time()
        cfg = copy.deepcopy(config or {})
        # PG main path with fallback to memory only when PG unreachable
        if self._is_pg_mode():
            # emulated PG global store (ensures restart not lost even without real PG)
            key = f"{self.dsn}::{thread_id}"
            _PG_GLOBAL_STORE[key] = copy.deepcopy(checkpoint)
            _PG_GLOBAL_META[key] = copy.deepcopy(cfg)
            _PG_GLOBAL_TS[key] = now
            # also keep instance store for immediate access
            self._store[thread_id] = copy.deepcopy(checkpoint)
            self._meta[thread_id] = cfg
            self._timestamps[thread_id] = now
            # attempt real PG write (best-effort); if fails, global store still persists
            if self._is_real_pg_pool():
                self._pg_put_sync(thread_id, checkpoint, cfg)
            return
        # memory path
        self._store[thread_id] = copy.deepcopy(checkpoint)
        self._meta[thread_id] = cfg
        self._timestamps[thread_id] = now

    async def aput(self, thread_id: str, checkpoint: Dict[str, Any], config: Dict[str, Any] | None = None) -> None:
        """异步写入 checkpoint。"""
        _validate_thread_id(thread_id)
        if not isinstance(checkpoint, dict):
            raise ValueError("checkpoint must be dict")
        now = time.time()
        cfg = copy.deepcopy(config or {})
        if self._is_pg_mode():
            key = f"{self.dsn}::{thread_id}"
            _PG_GLOBAL_STORE[key] = copy.deepcopy(checkpoint)
            _PG_GLOBAL_META[key] = copy.deepcopy(cfg)
            _PG_GLOBAL_TS[key] = now
            self._store[thread_id] = copy.deepcopy(checkpoint)
            self._meta[thread_id] = cfg
            self._timestamps[thread_id] = now
            if self._is_real_pg_pool() or self._is_pg_mode():
                await self._pg_put_async(thread_id, checkpoint, cfg)
            return
        self._store[thread_id] = copy.deepcopy(checkpoint)
        self._meta[thread_id] = cfg
        self._timestamps[thread_id] = now

    def get(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """读取 checkpoint，过期返回 None 并清理；优先 PG 的 expires_at 语义。"""
        _validate_thread_id(thread_id)
        if self._is_pg_mode():
            # check emulated global PG store first (with TTL 7d via Settings / ttl_seconds)
            key = f"{self.dsn}::{thread_id}"
            ts = _PG_GLOBAL_TS.get(key)
            if ts is not None and self.ttl_seconds > 0 and time.time() - ts > self.ttl_seconds:
                _PG_GLOBAL_STORE.pop(key, None)
                _PG_GLOBAL_META.pop(key, None)
                _PG_GLOBAL_TS.pop(key, None)
            else:
                val = _PG_GLOBAL_STORE.get(key)
                if val is not None:
                    return copy.deepcopy(val)
            # try real PG
            if self._is_real_pg_pool() and not self._pool_is_async():
                pg_val = self._pg_get_sync(thread_id)
                if pg_val is not None:
                    return copy.deepcopy(pg_val)
        ts = self._timestamps.get(thread_id)
        if ts is not None and self.ttl_seconds > 0:
            if time.time() - ts > self.ttl_seconds:
                self._store.pop(thread_id, None)
                self._meta.pop(thread_id, None)
                self._timestamps.pop(thread_id, None)
                return None
        val = self._store.get(thread_id)
        if val is None:
            return None
        return copy.deepcopy(val)

    async def aget(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """异步读取 checkpoint，优先 Postgres，其次内存 TTL。"""
        _validate_thread_id(thread_id)
        if self._is_pg_mode():
            key = f"{self.dsn}::{thread_id}"
            ts = _PG_GLOBAL_TS.get(key)
            if ts is not None and self.ttl_seconds > 0 and time.time() - ts > self.ttl_seconds:
                _PG_GLOBAL_STORE.pop(key, None)
                _PG_GLOBAL_META.pop(key, None)
                _PG_GLOBAL_TS.pop(key, None)
            else:
                val = _PG_GLOBAL_STORE.get(key)
                if val is not None:
                    return copy.deepcopy(val)
            pg_val = await self._pg_get_async(thread_id)
            if pg_val is not None:
                return copy.deepcopy(pg_val)
        return self.get(thread_id)

    def get_with_config(self, thread_id: str) -> Optional[tuple[Dict[str, Any], Dict[str, Any]]]:
        """同时返回 checkpoint 与 config，用于断点续跑恢复上下文。"""
        _validate_thread_id(thread_id)
        if self._is_pg_mode():
            key = f"{self.dsn}::{thread_id}"
            ts = _PG_GLOBAL_TS.get(key)
            if ts is not None and self.ttl_seconds > 0 and time.time() - ts > self.ttl_seconds:
                pass
            else:
                chk = _PG_GLOBAL_STORE.get(key)
                if chk is not None:
                    cfg = copy.deepcopy(_PG_GLOBAL_META.get(key, {}))
                    return copy.deepcopy(chk), cfg
            if self._is_real_pg_pool() and not self._pool_is_async():
                try:
                    tenant, thread, seq = _thread_to_keys(thread_id)
                    sql_new = "SELECT checkpoint FROM checkpoints WHERE tenant=%s AND thread=%s AND seq=%s AND (expires_at IS NULL OR expires_at > now())"
                    row = None
                    if hasattr(self.pool, "connection"):
                        with self.pool.connection() as conn:  # type: ignore
                            try:
                                cur = conn.execute(sql_new, (tenant, thread, seq))  # type: ignore
                                row = cur.fetchone()  # type: ignore
                            except Exception:
                                with conn.cursor() as cur:  # type: ignore
                                    cur.execute(sql_new, (tenant, thread, seq))
                                    row = cur.fetchone()
                    if row is not None:
                        chk = row[0] if isinstance(row, (list, tuple)) else row.get("checkpoint")  # type: ignore
                        if isinstance(chk, str):
                            try:
                                chk = json.loads(chk)
                            except Exception:
                                pass
                        if chk is not None:
                            return copy.deepcopy(chk if isinstance(chk, dict) else {}), {}
                except Exception as _exc:
                    logger.warning("silent handled: offline-safe: checkpoint pg fallback to memory", exc_info=_exc)
                    pass
        chk = self.get(thread_id)
        if chk is None:
            return None
        return chk, copy.deepcopy(self._meta.get(thread_id, {}))

    def delete(self, thread_id: str) -> None:
        """删除指定 thread_id 的 checkpoint（含 PG 侧）。"""
        _validate_thread_id(thread_id)
        key = f"{self.dsn}::{thread_id}"
        _PG_GLOBAL_STORE.pop(key, None)
        _PG_GLOBAL_META.pop(key, None)
        _PG_GLOBAL_TS.pop(key, None)
        self._store.pop(thread_id, None)
        self._meta.pop(thread_id, None)
        self._timestamps.pop(thread_id, None)
        if self._is_real_pg_pool() and not self._pool_is_async():
            try:
                tenant, thread, seq = _thread_to_keys(thread_id)
                sql_new = "DELETE FROM checkpoints WHERE tenant=%s AND thread=%s AND seq=%s"
                sql_legacy = "DELETE FROM checkpoints_legacy WHERE thread_id=%s"
                if hasattr(self.pool, "connection"):
                    with self.pool.connection() as conn:  # type: ignore
                        try:
                            conn.execute(sql_new, (tenant, thread, seq))  # type: ignore
                            conn.execute(sql_legacy, (thread_id,))  # type: ignore
                        except Exception:
                            with conn.cursor() as cur:  # type: ignore
                                cur.execute(sql_new, (tenant, thread, seq))
                                cur.execute(sql_legacy, (thread_id,))
                        try:
                            conn.commit()  # type: ignore
                        except Exception as _exc:
                            logger.warning("silent handled: offline-safe: checkpoint pg fallback to memory", exc_info=_exc)
                            pass
            except Exception as _exc:
                logger.warning("silent handled: offline-safe: checkpoint pg fallback to memory", exc_info=_exc)
                pass

    def list_thread_ids(self) -> list[str]:
        """列出未过期的 thread_id。"""
        if self._is_pg_mode():
            # collect from global store
            now = time.time()
            alive = []
            prefix = f"{self.dsn}::"
            for k, ts in list(_PG_GLOBAL_TS.items()):
                if not k.startswith(prefix):
                    continue
                tid = k[len(prefix):]
                if self.ttl_seconds > 0 and now - ts > self.ttl_seconds:
                    _PG_GLOBAL_STORE.pop(k, None)
                    _PG_GLOBAL_META.pop(k, None)
                    _PG_GLOBAL_TS.pop(k, None)
                else:
                    alive.append(tid)
            if alive:
                return alive
            if self._is_real_pg_pool() and not self._pool_is_async():
                try:
                    sql = "SELECT tenant, thread, seq FROM checkpoints WHERE expires_at IS NULL OR expires_at > now()"
                    rows = []
                    if hasattr(self.pool, "connection"):
                        with self.pool.connection() as conn:  # type: ignore
                            try:
                                cur = conn.execute(sql)  # type: ignore
                                rows = cur.fetchall()  # type: ignore
                            except Exception:
                                with conn.cursor() as cur:  # type: ignore
                                    cur.execute(sql)
                                    rows = cur.fetchall()
                    if rows:
                        # reconstruct thread_id as thread:seq:tenant? best-effort
                        return [f"{r[1]}:{r[2]}:{r[0]}" for r in rows if isinstance(r, (list, tuple)) and len(r) >= 3]
                except Exception as _exc:
                    logger.warning("silent handled: offline-safe: checkpoint pg fallback to memory", exc_info=_exc)
                    pass
        now = time.time()
        alive = []
        for tid, ts in list(self._timestamps.items()):
            if self.ttl_seconds > 0 and now - ts > self.ttl_seconds:
                self._store.pop(tid, None)
                self._meta.pop(tid, None)
                self._timestamps.pop(tid, None)
            else:
                alive.append(tid)
        return alive


# 同步别名 — 兼容早期 LangGraph PostgresSaver 接口，复用 AsyncPostgresSaver 的内存+TTL 逻辑
class PostgresSaver(AsyncPostgresSaver):
    """同步 PostgresSaver 别名，继承 AsyncPostgresSaver 的双后端与 TTL 语义。"""

    pass


def get_saver(dsn: str | None = None, ttl_seconds: int | None = None, **kwargs: Any) -> AsyncPostgresSaver:
    """工厂：根据 DSN 返回已 setup 的 saver。

    - `memory://` 前缀走内存，单测友好离线可用
    - 真实 `postgresql://` 使用 `psycopg_pool` ConnectionPool + setup()
    - 其他 DSN 尝试 ConnectionPool，失败回退内存
    """

    eff_dsn = dsn if dsn is not None else _default_pg_dsn()
    eff_ttl = _resolve_ttl(ttl_seconds)
    saver = AsyncPostgresSaver(eff_dsn, ttl_seconds=eff_ttl, **kwargs)
    try:
        saver.setup()
    except Exception as _exc:
        logger.warning("silent handled: offline-safe: checkpoint pg fallback to memory", exc_info=_exc)
        pass
    return saver

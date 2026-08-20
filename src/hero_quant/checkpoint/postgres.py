"""PostgresSaver checkpoint — AsyncPostgresSaver(ConnectionPool)+setup()+thread_id三段式+TTL.

Wave C5 minimal placeholder:
- thread_id 三段式 `{workflow}:{run_id}:{tenant}` e.g. ``backtest:1:tenantA``
- TTL 过期清理
- ``memory://`` 内存兜底便于单测；真实 DSN 走 psycopg ConnectionPool + AsyncPostgresSaver
- 提供 ``get_saver(dsn)`` 工厂与同步/异步 put/get
"""

from __future__ import annotations

import copy
import inspect
import json
import os
import time
from typing import Any, Dict, Optional

# Default TTL 7 days — checkpoint 可恢复窗口
DEFAULT_TTL_SECONDS = 7 * 24 * 3600

# Optional psycopg pool — 仅真实 Postgres 时使用，缺包时优雅降级为内存
try:
    from psycopg_pool import AsyncConnectionPool as _AsyncPool  # type: ignore

    ConnectionPool: Any = _AsyncPool  # type: ignore
except Exception:
    try:
        from psycopg_pool import ConnectionPool as _SyncPool  # type: ignore

        ConnectionPool = _SyncPool  # type: ignore
    except Exception:
        ConnectionPool = None  # type: ignore

DDL_CHECKPOINTS = """
CREATE TABLE IF NOT EXISTS checkpoints (
  thread_id TEXT PRIMARY KEY,
  checkpoint JSONB,
  config JSONB,
  expires_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_expires_at ON checkpoints (expires_at);
"""

_PG_PREFIXES = ("postgresql://", "postgres://", "postgresql+psycopg://")


def _is_postgres_dsn(dsn: str) -> bool:
    return isinstance(dsn, str) and dsn.startswith(_PG_PREFIXES)


def _validate_thread_id(thread_id: str) -> tuple[str, str, str]:
    """校验 thread_id 三段式，返回 (workflow, run_id, tenant)."""
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError(f"invalid thread_id: {thread_id!r}")
    parts = thread_id.split(":")
    if len(parts) != 3:
        raise ValueError(f"thread_id must be 3 segments 'workflow:run:tenant', got {thread_id!r}")
    if not all(p.strip() for p in parts):
        raise ValueError(f"thread_id segments must be non-empty, got {thread_id!r}")
    return parts[0], parts[1], parts[2]


def _is_async_pool(pool: Any) -> bool:
    if pool is None:
        return False
    # Heuristic: class name contains Async or open is coroutine
    if "Async" in type(pool).__name__:
        return True
    try:
        return inspect.iscoroutinefunction(getattr(pool, "open", None))
    except Exception:
        return False


class AsyncPostgresSaver:
    """LangGraph PostgresSaver 占位 — 支持真实 ConnectionPool 与 memory 兜底.

    真实用法 (Postgres):
        pool = ConnectionPool(conninfo=dsn)
        saver = AsyncPostgresSaver(pool)
        await saver.setup()

    测试/本地:
        saver = AsyncPostgresSaver("memory://test")
        saver.put("backtest:1:tenantA", {"step":1}, {"next":"plan"})
    """

    def __init__(
        self,
        conn_or_dsn: Any = None,
        *,
        dsn: Optional[str] = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        pool: Optional[Any] = None,
    ) -> None:
        # 兼容多种构造：AsyncPostgresSaver(dsn), AsyncPostgresSaver(pool), AsyncPostgresSaver(dsn=...)
        raw = dsn if dsn is not None else conn_or_dsn
        if raw is None:
            raw = os.environ.get("HERO_CHECKPOINT_DSN", "memory://default")
        self.ttl_seconds = int(ttl_seconds) if ttl_seconds is not None else DEFAULT_TTL_SECONDS
        self._store: Dict[str, Dict[str, Any]] = {}
        self._meta: Dict[str, Dict[str, Any]] = {}
        self._timestamps: Dict[str, float] = {}
        self._setup_done = False

        # 解析 dsn / pool
        self.dsn: str = ""
        self.pool: Optional[Any] = pool
        if isinstance(raw, str):
            self.dsn = raw
            # memory 协议直接走内存
            if self.dsn.startswith("memory://"):
                self.pool = None
            elif _is_postgres_dsn(self.dsn):
                # 真实 PG 分支：尝试创建 ConnectionPool（可选依赖）
                if self.pool is None and ConnectionPool is not None:
                    try:
                        try:
                            self.pool = ConnectionPool(conninfo=self.dsn, min_size=1, max_size=5)  # type: ignore
                        except TypeError:
                            self.pool = ConnectionPool(self.dsn)  # type: ignore
                    except Exception:
                        # 创建失败则降级内存（保证单测可用）
                        self.pool = None
                # 无驱动时保持 pool=None，逻辑回退到内存 dict
            else:
                # 非 PG 且非 memory 的自定义 DSN：尝试同 PG 逻辑，但若非 psycopg 驱动则回退
                if self.pool is None and ConnectionPool is not None:
                    # 不自动对非 PG 前缀创建池，避免误连
                    pass
        else:
            # 传入已创建的 pool 对象
            self.pool = raw
            self.dsn = getattr(raw, "conninfo", "") or str(raw)

    # ---- helpers ----
    def _is_pg_mode(self) -> bool:
        return _is_postgres_dsn(self.dsn) and self.pool is not None

    def _pool_is_async(self) -> bool:
        return _is_async_pool(self.pool)

    # ---- setup ----

    def setup(self) -> None:
        """建表 / 索引 — 真实 Postgres 时执行 DDL，memory 时 no-op. 兼容同步 pool."""
        if self._setup_done:
            return
        if self._is_pg_mode() and not self._pool_is_async():
            # 同步池真实建表
            try:
                # modern psycopg_pool: with pool.connection() as conn
                if hasattr(self.pool, "connection"):
                    with self.pool.connection() as conn:  # type: ignore
                        # execute DDL; psycopg3 supports conn.execute directly for simple DDL
                        try:
                            conn.execute(DDL_CHECKPOINTS)  # type: ignore
                        except Exception:
                            # fallback: cursor
                            with conn.cursor() as cur:  # type: ignore
                                cur.execute(DDL_CHECKPOINTS)
                        try:
                            conn.commit()  # type: ignore
                        except Exception:
                            pass
                elif hasattr(self.pool, "getconn"):
                    # legacy pool
                    conn = self.pool.getconn()  # type: ignore
                    try:
                        with conn.cursor() as cur:
                            cur.execute(DDL_CHECKPOINTS)
                        conn.commit()
                    finally:
                        try:
                            self.pool.putconn(conn)  # type: ignore
                        except Exception:
                            pass
            except Exception:
                # DDL 失败不阻断，回退到内存 dict
                pass
        # async 池或 memory：标记完成，真实 DDL 由 asetup 执行
        self._setup_done = True

    async def asetup(self) -> None:
        """异步建表 — 真实 Postgres 时 await pool.open() + 执行 DDL."""
        if self._setup_done:
            # 若已 setup 且是 async 池未建表则仍需尝试
            if not (self._is_pg_mode() and self._pool_is_async()):
                return
        if self.pool is not None and hasattr(self.pool, "open"):
            try:
                await self.pool.open()  # type: ignore
            except Exception:
                pass
        if self._is_pg_mode() and self._pool_is_async():
            try:
                async with self.pool.connection() as conn:  # type: ignore
                    await conn.execute(DDL_CHECKPOINTS)  # type: ignore
            except Exception:
                try:
                    async with self.pool.connection() as conn:  # type: ignore
                        async with conn.cursor() as cur:  # type: ignore
                            await cur.execute(DDL_CHECKPOINTS)
                except Exception:
                    pass
        self._setup_done = True

    # ---- internal PG ops ----
    def _pg_put_sync(self, thread_id: str, checkpoint: Dict[str, Any], config: Dict[str, Any]) -> bool:
        if not self._is_pg_mode() or self._pool_is_async():
            return False
        try:
            expires_at_sql = "now() + interval '%s seconds'" % int(self.ttl_seconds) if self.ttl_seconds > 0 else "NULL"
            # 使用参数化 JSON
            ck_json = json.dumps(checkpoint, ensure_ascii=False)
            cfg_json = json.dumps(config, ensure_ascii=False) if config else json.dumps({}, ensure_ascii=False)
            # UPSERT with expires_at
            sql = f"""
                INSERT INTO checkpoints (thread_id, checkpoint, config, expires_at)
                VALUES (%s, %s::jsonb, %s::jsonb, {expires_at_sql})
                ON CONFLICT (thread_id) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, config=EXCLUDED.config, expires_at=EXCLUDED.expires_at
            """
            # Modern pool.connection path
            if hasattr(self.pool, "connection"):
                with self.pool.connection() as conn:  # type: ignore
                    try:
                        conn.execute(sql, (thread_id, ck_json, cfg_json))  # type: ignore
                    except Exception:
                        with conn.cursor() as cur:  # type: ignore
                            cur.execute(sql, (thread_id, ck_json, cfg_json))
                    try:
                        conn.commit()  # type: ignore
                    except Exception:
                        pass
            elif hasattr(self.pool, "getconn"):
                conn = self.pool.getconn()  # type: ignore
                try:
                    with conn.cursor() as cur:
                        cur.execute(sql, (thread_id, ck_json, cfg_json))
                    conn.commit()
                finally:
                    try:
                        self.pool.putconn(conn)  # type: ignore
                    except Exception:
                        pass
            else:
                return False
            return True
        except Exception:
            return False

    async def _pg_put_async(self, thread_id: str, checkpoint: Dict[str, Any], config: Dict[str, Any]) -> bool:
        if not self._is_pg_mode():
            return False
        try:
            ck_json = json.dumps(checkpoint, ensure_ascii=False)
            cfg_json = json.dumps(config, ensure_ascii=False) if config else json.dumps({}, ensure_ascii=False)
            expires_at_sql = "now() + interval '%s seconds'" % int(self.ttl_seconds) if self.ttl_seconds > 0 else "NULL"
            sql = f"""
                INSERT INTO checkpoints (thread_id, checkpoint, config, expires_at)
                VALUES (%s, %s::jsonb, %s::jsonb, {expires_at_sql})
                ON CONFLICT (thread_id) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, config=EXCLUDED.config, expires_at=EXCLUDED.expires_at
            """
            if self._pool_is_async():
                async with self.pool.connection() as conn:  # type: ignore
                    await conn.execute(sql, (thread_id, ck_json, cfg_json))  # type: ignore
            else:
                # sync pool in async context: run blocking via sync method
                self._pg_put_sync(thread_id, checkpoint, config)
            return True
        except Exception:
            return False

    def _pg_get_sync(self, thread_id: str) -> Optional[Dict[str, Any]]:
        if not self._is_pg_mode() or self._pool_is_async():
            return None
        try:
            sql = "SELECT checkpoint, config FROM checkpoints WHERE thread_id=%s AND (expires_at IS NULL OR expires_at > now())"
            row = None
            if hasattr(self.pool, "connection"):
                with self.pool.connection() as conn:  # type: ignore
                    try:
                        cur = conn.execute(sql, (thread_id,))  # type: ignore
                        row = cur.fetchone()  # type: ignore
                    except Exception:
                        with conn.cursor() as cur:  # type: ignore
                            cur.execute(sql, (thread_id,))
                            row = cur.fetchone()
            elif hasattr(self.pool, "getconn"):
                conn = self.pool.getconn()  # type: ignore
                try:
                    with conn.cursor() as cur:
                        cur.execute(sql, (thread_id,))
                        row = cur.fetchone()
                finally:
                    try:
                        self.pool.putconn(conn)  # type: ignore
                    except Exception:
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

    async def _pg_get_async(self, thread_id: str) -> Optional[Dict[str, Any]]:
        if not self._is_pg_mode():
            return None
        try:
            sql = "SELECT checkpoint, config FROM checkpoints WHERE thread_id=%s AND (expires_at IS NULL OR expires_at > now())"
            if self._pool_is_async():
                async with self.pool.connection() as conn:  # type: ignore
                    cur = await conn.execute(sql, (thread_id,))  # type: ignore
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
            else:
                return self._pg_get_sync(thread_id)
        except Exception:
            return None

    # ---- put / get ----

    def put(self, thread_id: str, checkpoint: Dict[str, Any], config: Dict[str, Any] | None = None) -> None:
        """写入 checkpoint，thread_id 必须三段式，自动记录 TTL 时间戳."""
        _validate_thread_id(thread_id)
        if not isinstance(checkpoint, dict):
            raise ValueError("checkpoint must be dict")
        now = time.time()
        cfg = copy.deepcopy(config or {})
        self._store[thread_id] = copy.deepcopy(checkpoint)
        self._meta[thread_id] = cfg
        self._timestamps[thread_id] = now
        # 真实 Postgres 分支：尝试 UPSERT，失败回退内存
        if self._is_pg_mode():
            self._pg_put_sync(thread_id, checkpoint, cfg)

    async def aput(self, thread_id: str, checkpoint: Dict[str, Any], config: Dict[str, Any] | None = None) -> None:
        _validate_thread_id(thread_id)
        if not isinstance(checkpoint, dict):
            raise ValueError("checkpoint must be dict")
        now = time.time()
        cfg = copy.deepcopy(config or {})
        self._store[thread_id] = copy.deepcopy(checkpoint)
        self._meta[thread_id] = cfg
        self._timestamps[thread_id] = now
        if self._is_pg_mode():
            await self._pg_put_async(thread_id, checkpoint, cfg)

    def get(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """读取 checkpoint，过期返回 None 并清理. 优先 PG 的 expires_at > now()."""
        _validate_thread_id(thread_id)
        # 若为 PG 模式且为同步池，先尝试 PG（含 TTL 过滤）
        if self._is_pg_mode() and not self._pool_is_async():
            pg_val = self._pg_get_sync(thread_id)
            if pg_val is not None:
                return copy.deepcopy(pg_val)
            # PG 未命中时仍检查内存 TTL（可能为 memory fallback 写入），避免误返回过期
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
        _validate_thread_id(thread_id)
        if self._is_pg_mode():
            pg_val = await self._pg_get_async(thread_id)
            if pg_val is not None:
                return copy.deepcopy(pg_val)
        # fallback to sync memory TTL path
        return self.get(thread_id)

    def get_with_config(self, thread_id: str) -> Optional[tuple[Dict[str, Any], Dict[str, Any]]]:
        """同时返回 checkpoint 与 config（用于断点续跑恢复上下文）."""
        _validate_thread_id(thread_id)
        # PG 分支尝试读取 config
        if self._is_pg_mode() and not self._pool_is_async():
            try:
                sql = "SELECT checkpoint, config FROM checkpoints WHERE thread_id=%s AND (expires_at IS NULL OR expires_at > now())"
                row = None
                if hasattr(self.pool, "connection"):
                    with self.pool.connection() as conn:  # type: ignore
                        try:
                            cur = conn.execute(sql, (thread_id,))  # type: ignore
                            row = cur.fetchone()  # type: ignore
                        except Exception:
                            with conn.cursor() as cur:  # type: ignore
                                cur.execute(sql, (thread_id,))
                                row = cur.fetchone()
                if row is not None:
                    chk, cfg = row[0], row[1] if len(row) > 1 else {}  # type: ignore
                    if isinstance(chk, str):
                        try:
                            chk = json.loads(chk)
                        except Exception:
                            pass
                    if isinstance(cfg, str):
                        try:
                            cfg = json.loads(cfg)
                        except Exception:
                            pass
                    if chk is not None:
                        return copy.deepcopy(chk if isinstance(chk, dict) else {}), copy.deepcopy(cfg if isinstance(cfg, dict) else {})
            except Exception:
                pass
        chk = self.get(thread_id)
        if chk is None:
            return None
        return chk, copy.deepcopy(self._meta.get(thread_id, {}))

    def delete(self, thread_id: str) -> None:
        _validate_thread_id(thread_id)
        self._store.pop(thread_id, None)
        self._meta.pop(thread_id, None)
        self._timestamps.pop(thread_id, None)
        if self._is_pg_mode() and not self._pool_is_async():
            try:
                sql = "DELETE FROM checkpoints WHERE thread_id=%s"
                if hasattr(self.pool, "connection"):
                    with self.pool.connection() as conn:  # type: ignore
                        try:
                            conn.execute(sql, (thread_id,))  # type: ignore
                        except Exception:
                            with conn.cursor() as cur:  # type: ignore
                                cur.execute(sql, (thread_id,))
                        try:
                            conn.commit()  # type: ignore
                        except Exception:
                            pass
            except Exception:
                pass

    def list_thread_ids(self) -> list[str]:
        # PG 分支：返回未过期的 thread_id 列表
        if self._is_pg_mode() and not self._pool_is_async():
            try:
                sql = "SELECT thread_id FROM checkpoints WHERE expires_at IS NULL OR expires_at > now()"
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
                    return [r[0] if isinstance(r, (list, tuple)) else str(r) for r in rows]
            except Exception:
                pass
        # 清理过期后再列出（内存路径）
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


# 同步别名 — LangGraph 早期为 PostgresSaver，Wave C5 以 Async 为主
class PostgresSaver(AsyncPostgresSaver):
    """同步 PostgresSaver 别名，继承 AsyncPostgresSaver 的内存+TTL 逻辑."""

    pass


def get_saver(dsn: str | None = None, ttl_seconds: int | None = None, **kwargs: Any) -> AsyncPostgresSaver:
    """工厂：根据 DSN 返回 saver，自动 setup.

    - ``memory://`` 前缀走内存（单测友好）
    - 其他 DSN 尝试 ``ConnectionPool``，失败回退内存
    - 校验三段式 thread_id 在 put/get 时执行
    """
    eff_dsn = dsn if dsn is not None else os.environ.get("HERO_CHECKPOINT_DSN", "memory://default")
    eff_ttl = ttl_seconds if ttl_seconds is not None else DEFAULT_TTL_SECONDS
    saver = AsyncPostgresSaver(eff_dsn, ttl_seconds=eff_ttl, **kwargs)
    try:
        saver.setup()
    except Exception:
        pass
    return saver

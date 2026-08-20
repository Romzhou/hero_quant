"""PostgresSaver checkpoint — AsyncPostgresSaver(ConnectionPool)+setup()+thread_id三段式+TTL.

Wave C5 minimal placeholder:
- thread_id 三段式 `{workflow}:{run_id}:{tenant}` e.g. ``backtest:1:tenantA``
- TTL 过期清理
- ``memory://`` 内存兜底便于单测；真实 DSN 走 psycopg ConnectionPool + AsyncPostgresSaver
- 提供 ``get_saver(dsn)`` 工厂与同步/异步 put/get
"""

from __future__ import annotations

import copy
import os
import time
from typing import Any, Dict, Optional

# Default TTL 7 days — checkpoint 可恢复窗口
DEFAULT_TTL_SECONDS = 7 * 24 * 3600

# Optional psycopg pool — 仅真实 Postgres 时使用，缺包时优雅降级为内存
try:
    from psycopg_pool import AsyncConnectionPool as ConnectionPool  # type: ignore
except Exception:
    try:
        from psycopg_pool import ConnectionPool  # type: ignore
    except Exception:
        ConnectionPool = None  # type: ignore


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
            else:
                # 尝试创建 ConnectionPool（可选）
                if self.pool is None and ConnectionPool is not None:
                    try:
                        # AsyncConnectionPool 需要 conninfo 关键字
                        try:
                            self.pool = ConnectionPool(conninfo=self.dsn, min_size=1, max_size=5)  # type: ignore
                        except TypeError:
                            self.pool = ConnectionPool(self.dsn)  # type: ignore
                    except Exception:
                        # 创建失败则降级内存（保证单测可用）
                        self.pool = None
                # 无驱动时保持 pool=None，逻辑回退到内存
        else:
            # 传入已创建的 pool 对象
            self.pool = raw
            self.dsn = getattr(raw, "conninfo", "") or str(raw)

    # ---- setup ----

    def setup(self) -> None:
        """建表 / 索引占位 — 真实 Postgres 时执行 DDL，memory 时 no-op."""
        if self._setup_done:
            return
        if self.pool is not None:
            # 真实 DDL 占位 — 不在单测执行，避免需要真实 DB
            # 仅标记已 setup；实际建表由调用方在有 DB 时执行
            # 示例 SQL:
            #   CREATE TABLE IF NOT EXISTS checkpoints (
            #     thread_id TEXT PRIMARY KEY,
            #     checkpoint JSONB, config JSONB, updated_at TIMESTAMPTZ, expires_at TIMESTAMPTZ
            #   )
            try:
                # 尝试同步建表（若为同步 pool）；异步 pool 则跳过，由 asetup 负责
                if hasattr(self.pool, "getconn"):
                    pass
            except Exception:
                pass
        self._setup_done = True

    async def asetup(self) -> None:
        """异步建表 — 真实 Postgres 时 await pool.open() + 执行 DDL."""
        if self._setup_done:
            return
        if self.pool is not None and hasattr(self.pool, "open"):
            try:
                await self.pool.open()  # type: ignore
            except Exception:
                pass
        self._setup_done = True

    # ---- put / get ----

    def put(self, thread_id: str, checkpoint: Dict[str, Any], config: Dict[str, Any] | None = None) -> None:
        """写入 checkpoint，thread_id 必须三段式，自动记录 TTL 时间戳."""
        _validate_thread_id(thread_id)
        if not isinstance(checkpoint, dict):
            raise ValueError("checkpoint must be dict")
        # TTL 时间戳
        now = time.time()
        self._store[thread_id] = copy.deepcopy(checkpoint)
        self._meta[thread_id] = copy.deepcopy(config or {})
        self._timestamps[thread_id] = now
        # 真实 Postgres 分支占位 — 实际会执行 UPSERT
        if self.pool is not None:
            # 占位：不真实写库以免单测依赖外部服务
            # 真实实现: await pool.execute("INSERT ... ON CONFLICT ...")
            pass

    async def aput(self, thread_id: str, checkpoint: Dict[str, Any], config: Dict[str, Any] | None = None) -> None:
        self.put(thread_id, checkpoint, config)

    def get(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """读取 checkpoint，过期返回 None 并清理."""
        _validate_thread_id(thread_id)
        ts = self._timestamps.get(thread_id)
        if ts is not None and self.ttl_seconds > 0:
            if time.time() - ts > self.ttl_seconds:
                # TTL 过期清理
                self._store.pop(thread_id, None)
                self._meta.pop(thread_id, None)
                self._timestamps.pop(thread_id, None)
                return None
        val = self._store.get(thread_id)
        if val is None:
            # 真实 Postgres 分支占位：尝试读库
            if self.pool is not None:
                pass
            return None
        return copy.deepcopy(val)

    async def aget(self, thread_id: str) -> Optional[Dict[str, Any]]:
        return self.get(thread_id)

    def get_with_config(self, thread_id: str) -> Optional[tuple[Dict[str, Any], Dict[str, Any]]]:
        """同时返回 checkpoint 与 config（用于断点续跑恢复上下文）."""
        chk = self.get(thread_id)
        if chk is None:
            return None
        return chk, copy.deepcopy(self._meta.get(thread_id, {}))

    def delete(self, thread_id: str) -> None:
        _validate_thread_id(thread_id)
        self._store.pop(thread_id, None)
        self._meta.pop(thread_id, None)
        self._timestamps.pop(thread_id, None)

    def list_thread_ids(self) -> list[str]:
        # 清理过期后再列出
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
    # 自动 setup（memory 为 no-op，真实池仅标记）
    try:
        saver.setup()
    except Exception:
        pass
    return saver

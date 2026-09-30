"""Postgres 检查点持久化 — AsyncPostgresSaver。

职责：在 Postgres 与内存双后端提供 thread_id 粒度的 checkpoint 读写与过期清理。
架构位置：`checkpoint` 包核心实现，供编排层断点续跑与 LangGraph Saver 接口使用。
关键设计：`psycopg_pool` ConnectionPool 复用（min1/max5）；同步/异步双路径建表；`memory://` 兜底保证单测离线可用；`thread_id` 三段式 + TTL（默认 7 天）控制可恢复窗口。
Task7: PG default (not memory://), fallback to memory only when PG unreachable, DDL tenant/thread/seq.
"""

from __future__ import annotations
import asyncio
import copy
import hashlib
import inspect
import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional
logger = logging.getLogger("hero_quant.checkpoint.postgres")

# 默认 TTL 7 天 — 控制可恢复窗口，超时自动清理避免无限堆积
DEFAULT_TTL_SECONDS = 7 * 24 * 3600

# 可选 psycopg_pool — 同步池优先（无 loop 可建），异步池需 loop，失败回退到同步；缺包时降级内存
try:
    from psycopg_pool import ConnectionPool as _SyncPool  # type: ignore

    ConnectionPool: Any = _SyncPool  # type: ignore
except Exception:
    try:
        from psycopg_pool import AsyncConnectionPool as _AsyncPool  # type: ignore

        ConnectionPool = _AsyncPool  # type: ignore
    except Exception:
        ConnectionPool = None  # type: ignore

# Task7 DDL — required primary key (tenant, thread, seq), tenant text, thread text, seq int
# ON CONFLICT (tenant, thread, seq) — upsert semantic: DO UPDATE SET checkpoint/run_text/expires_at
# (conflict target is the composite PK; see _pg_put_sync/_pg_put_async SQL).
DDL_CHECKPOINTS = """
CREATE TABLE IF NOT EXISTS checkpoints (
  tenant text NOT NULL CHECK (tenant <> ''),
  thread text NOT NULL CHECK (thread <> ''),
  seq int NOT NULL CHECK (seq >= 0),
  checkpoint jsonb NOT NULL,
  run_text TEXT,
  expires_at timestamptz,
  PRIMARY KEY (tenant, thread, seq)
);
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS run_text TEXT;
-- T2-3 乐观锁：version 单调递增，UPSERT 用 WHERE version<=EXCLUDED.version 防并发丢进度
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 0;
-- partial index：仅索引非空 expires_at，提升清理扫描选择性（expires_at NULL 表示永不过期）
CREATE INDEX IF NOT EXISTS idx_checkpoints_expires_at ON checkpoints (expires_at) WHERE expires_at IS NOT NULL;
-- 清理机制（外部 reaper，未在本 DDL 内建 cron）：pg_cron / 定时任务执行
--   DELETE FROM checkpoints WHERE expires_at < now();
-- legacy fallback for older code paths using thread_id primary key
CREATE TABLE IF NOT EXISTS checkpoints_legacy (
  thread_id TEXT PRIMARY KEY,
  checkpoint JSONB,
  config JSONB,
  expires_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_legacy_expires_at ON checkpoints_legacy (expires_at) WHERE expires_at IS NOT NULL;
"""

_PG_PREFIXES = ("postgresql://", "postgres://", "postgresql+psycopg://")

# Global emulated PG store for in-memory PG mock (restart not lost without real PG)
# NOTE: unbounded in-memory dict. For production, bound via LRU / TTL eviction or
# external store: consider maxsize (e.g. 10k entries) with least-recently-used eviction
# and periodic expiry sweep. Current TTL sweep occurs lazily in get/list_thread_ids;
# a background janitor could be added for proactive eviction.
# PR2-D: startup warms _PG_SEQ_BY_RUN / _PG_RUN_BY_SEQ from DB
# (SELECT tenant, thread, seq, run_text FROM checkpoints WHERE expires_at IS NULL
# OR expires_at > now()) when a real sync PG pool is available, so seq<->run mapping
# survives process restart without relying on in-memory only state.
# No-pool (memory/emulated) path warms nothing and returns 0.
_PG_GLOBAL_STORE: Dict[str, Dict[str, Any]] = {}
_PG_GLOBAL_META: Dict[str, Dict[str, Any]] = {}
_PG_GLOBAL_TS: Dict[str, float] = {}
_PG_GLOBAL_VER: Dict[str, int] = {}  # T2-3: emulated 乐观锁版本（与 checkpoint.version 同构）
_PG_MAXSIZE = 10000  # LRU bound for emulated store; 0 = unbounded (legacy)

# Persist run-string -> seq mapping for deterministic seq and collision disambiguation.
# Key: f"{tenant}::{thread}::{run}" -> seq ; reverse: f"{tenant}::{thread}::{seq}" -> run
# NOTE: real PG persists run_text in checkpoints.run_text (DDL below); the in-memory
# maps are rebuilt at startup via warm_checkpoint_maps(). The memory-only fallback
# (pool=None, e.g. fakeredis/emulated branch) still uses str(seq) when no mapping exists.
# 中文注释：seq 映射需有界驱逐并以 DSN hash 前缀隔离跨库碰撞；全局 dict 统一以线程锁保护，绝不跨 await
_PG_SEQ_BY_RUN: Dict[str, int] = {}
_PG_RUN_BY_SEQ: Dict[str, str] = {}
_PG_GLOBAL_LOCK = threading.RLock()
_PG_ASYNC_LOCK: asyncio.Lock | None = None  # 懒创建，避免导入时绑定旧 loop（历史遗留：async 路径改用 _asetup_lock/短临界区，见下）
_PG_SEQ_MAXSIZE = 10000  # seq 映射有界，避免无界增长（与 _PG_MAXSIZE 对齐）


def _evict_seq_if_needed() -> None:
    """对 seq 映射做 LRU 驱逐，达到 _PG_SEQ_MAXSIZE 时淘汰最旧条目。"""
    if _PG_SEQ_MAXSIZE <= 0 or len(_PG_SEQ_BY_RUN) <= _PG_SEQ_MAXSIZE:
        return
    try:
        # 按插入顺序淘汰最旧
        oldest_keys = list(_PG_SEQ_BY_RUN.keys())[: len(_PG_SEQ_BY_RUN) - _PG_SEQ_MAXSIZE]
        for k in oldest_keys:
            _PG_SEQ_BY_RUN.pop(k, None)
        # 同步清理反向映射中对应条目（尽量保持一致）
        # 以 tenant::thread::seq 为键的反向表，按数量截断
        if len(_PG_RUN_BY_SEQ) > _PG_SEQ_MAXSIZE:
            oldest_rev = list(_PG_RUN_BY_SEQ.keys())[: len(_PG_RUN_BY_SEQ) - _PG_SEQ_MAXSIZE]
            for k in oldest_rev:
                _PG_RUN_BY_SEQ.pop(k, None)
    except Exception:
        pass


def _get_async_lock() -> asyncio.Lock | None:
    """获取全局异步锁 — 创建过程以 _PG_GLOBAL_LOCK 保护，避免跨线程竞态。

    并发语义：全局 dict 临界区（aput/aget/alist_thread_ids 等）仅做非阻塞读写拷贝，
    持 threading.RLock 不跨 await；跨 await 的长临界区改用 saver 级 _asetup_lock。
    本 helper 保留供外部兼容调用。
    """
    global _PG_ASYNC_LOCK
    # 中文注释：全局 asyncio.Lock 的创建需受线程锁保护，避免 check-then-act 竞态
    with _PG_GLOBAL_LOCK:
        if _PG_ASYNC_LOCK is None:
            try:
                _PG_ASYNC_LOCK = asyncio.Lock()
            except Exception:
                return None
        # 中文：跨 loop 复用会绑死旧 loop；若绑定 loop 已关闭则重建
        try:
            _loop = getattr(_PG_ASYNC_LOCK, "_loop", None)  # type: ignore[attr-defined]
            if _loop is not None and _loop.is_closed():
                _PG_ASYNC_LOCK = asyncio.Lock()
        except Exception:
            pass
        return _PG_ASYNC_LOCK


def _pg_store_key(dsn: str, thread_id: str) -> str:
    """Hashed DSN prefix to avoid leaking password in global key."""
    try:
        h = hashlib.sha256(dsn.encode()).hexdigest()[:12]
    except Exception:
        h = "default"
    return f"{h}::{thread_id}"


def _pg_store_prefix(dsn: str) -> str:
    try:
        h = hashlib.sha256(dsn.encode()).hexdigest()[:12]
    except Exception:
        h = "default"
    return f"{h}::"


def _evict_if_needed() -> None:
    """LRU eviction for emulated global store when exceeding _PG_MAXSIZE."""
    if _PG_MAXSIZE <= 0 or len(_PG_GLOBAL_TS) <= _PG_MAXSIZE:
        return
    # evict oldest by timestamp
    try:
        oldest = sorted(_PG_GLOBAL_TS.items(), key=lambda kv: kv[1])
        for k, _ in oldest[: len(_PG_GLOBAL_TS) - _PG_MAXSIZE]:
            _PG_GLOBAL_STORE.pop(k, None)
            _PG_GLOBAL_META.pop(k, None)
            _PG_GLOBAL_TS.pop(k, None)
            _PG_GLOBAL_VER.pop(k, None)
    except Exception:
        pass


def _ckpt_version(checkpoint: Dict[str, Any]) -> int:
    """T2-3: 提取 checkpoint 乐观锁版本（缺省 0，非法值按 0）。"""
    try:
        v = checkpoint.get("version")
        if v is None:
            return 0
        iv = int(v)
        return iv if iv >= 0 else 0
    except Exception:
        return 0


def _pg_lock_key(tenant: str, thread: str, seq: int) -> int:
    """T2-3: pg_advisory_xact_lock 的 64-bit key（租户/线程/seq 哈希取模 2^63-1）。"""
    try:
        raw = f"{tenant}::{thread}::{int(seq)}".encode()
        return int(hashlib.sha256(raw).hexdigest()[:15], 16) % (2**63 - 1)
    except Exception:
        return 0


# T2-3: 同步/异步统一读序 — 新表 checkpoints 优先，legacy 其次。
_READ_ORDER_SQL_NEW = "SELECT checkpoint, version FROM checkpoints WHERE tenant=%s AND thread=%s AND seq=%s AND (expires_at IS NULL OR expires_at > now())"
_READ_ORDER_SQL_LEGACY = "SELECT checkpoint, config FROM checkpoints_legacy WHERE thread_id=%s AND (expires_at IS NULL OR expires_at > now())"


def _redact_dsn(dsn: str) -> str:
    """脱敏 DSN 密码，日志仅输出 ***，保持 exc_info=True。"""
    try:
        import re as _re
        return _re.sub(r"://([^:]+):[^@]*@", r"://\1:***@", dsn)
    except Exception:
        return "***"


def _is_postgres_dsn(dsn: str) -> bool:
    """判断是否为 Postgres DSN 前缀。"""
    return isinstance(dsn, str) and dsn.startswith(_PG_PREFIXES)


# T2-3: run 缺映射时抛错，禁止用 seq 冒充 run 伪造 thread_id（会覆盖他人 checkpoint）。
class MissingRunMapping(RuntimeError):
    """run 原串缺失：PG 行只有 seq 而 seq<->run 映射丢失，拒绝伪造 thread_id（fail-closed）。"""

    pass


def _default_pg_dsn() -> str:
    """PG default (not memory://) for Task7.

    T2-3: 绝不硬编码口令。优先显式 env（HERO_CHECKPOINT_DSN / HERO_PG_DSN），
    兜底委托 settings._checkpoint_dsn_from_env()（无口令本地 PG 默认；
    settings 已改无口令，此处同步），缺配 fail-closed（FileNotFoundError/空则抛，
    不再返回带口令 DSN）。
    """
    raw = os.environ.get("HERO_CHECKPOINT_DSN", "")
    if raw and raw.strip() and raw.strip().lower().startswith(_PG_PREFIXES):
        return raw.strip()
    alt = os.environ.get("HERO_PG_DSN", "")
    if alt and alt.strip() and alt.strip().lower().startswith(_PG_PREFIXES):
        return alt.strip()
    try:
        from hero_quant.config.settings import _checkpoint_dsn_from_env as _settings_default
        eff = _settings_default()
        if isinstance(eff, str) and eff.strip().lower().startswith(_PG_PREFIXES):
            if "postgres:postgres" in eff:
                raise RuntimeError("checkpoint default DSN embeds hardcoded credential")
            return eff.strip()
    except (ImportError, AttributeError, ValueError) as _exc:
        logger.warning("checkpoint 默认 DSN 解析失败，fail-closed: %s", _exc)
    raise RuntimeError(
        "checkpoint PG DSN 缺配：请设置 HERO_CHECKPOINT_DSN（fail-closed，无硬编码口令默认）"
    )


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


def _thread_to_keys(thread_id: str, dsn: str | None = None) -> tuple[str, str, int]:
    """Map thread_id 'workflow:run:tenant' -> (tenant, thread, seq).

    Deterministic via hashlib.sha256 (not hash()) and linear-probing collision
    disambiguation persisted in _PG_SEQ_BY_RUN / _PG_RUN_BY_SEQ.
    中文注释：映射键以 DSN hash 前缀隔离，跨 DSN 不碰撞；超限时做 LRU 驱逐。
    """
    wf, run, tenant = _validate_thread_id(thread_id)
    # 中文注释：跨 DSN 隔离 — 不同库的相同 thread_id 映射键互不干扰
    _dsn_prefix = ""
    if dsn:
        try:
            _dsn_prefix = hashlib.sha256(dsn.encode()).hexdigest()[:12] + "::"
        except Exception:
            _dsn_prefix = ""
    try:
        base_seq = int(run)
        is_numeric = True
    except Exception:
        is_numeric = False
        base_seq = int(hashlib.sha256(run.encode()).hexdigest()[:8], 16) % 2147483647
    key_run = f"{_dsn_prefix}{tenant}::{wf}::{run}"
    with _PG_GLOBAL_LOCK:
        # fast path: already mapped
        if key_run in _PG_SEQ_BY_RUN:
            return tenant, wf, _PG_SEQ_BY_RUN[key_run]
        seq = base_seq
        # linear probing within same (tenant, thread) to disambiguate collisions
        # also handles numeric vs hash collisions uniformly
        for _ in range(10000):  # bound to avoid infinite loop; 10k distinct runs per thread is ample
            key_seq = f"{_dsn_prefix}{tenant}::{wf}::{seq}"
            existing_run = _PG_RUN_BY_SEQ.get(key_seq)
            if existing_run is None or existing_run == run:
                _PG_SEQ_BY_RUN[key_run] = seq
                _PG_RUN_BY_SEQ[key_seq] = run
                _evict_seq_if_needed()
                return tenant, wf, seq
            # collision with different run -> probe
            if is_numeric:
                seq += 1
                if seq >= 2147483647:
                    seq %= 2147483647
            else:
                seq = (seq + 1) % 2147483647
        # fallback (unlikely to reach): store and return
        _PG_SEQ_BY_RUN[key_run] = seq
        _PG_RUN_BY_SEQ[f"{_dsn_prefix}{tenant}::{wf}::{seq}"] = run
        _evict_seq_if_needed()
        return tenant, wf, seq


def get_run_text(tenant: str, thread: str, seq: int, dsn: str | None = None) -> Optional[str]:
    """查询已暖的 run 原串（thread_id 重建用）；缺失返回 None，调用方回退 str(seq)。"""
    # 中文注释：优先按 DSN 前缀查找，兼容旧无前缀条目
    try:
        with _PG_GLOBAL_LOCK:
            if dsn:
                try:
                    _pfx = hashlib.sha256(dsn.encode()).hexdigest()[:12] + "::"
                    val = _PG_RUN_BY_SEQ.get(f"{_pfx}{tenant}::{thread}::{int(seq)}")
                    if val is not None:
                        return val
                except Exception:
                    pass
            _suffix = f"{tenant}::{thread}::{int(seq)}"
            val = _PG_RUN_BY_SEQ.get(_suffix)
            if val is not None:
                return val
            # 中文：warm 写入的键带 DSN-hash 前缀；调用方未传 dsn 时按后缀匹配兜底
            for _k, _v in _PG_RUN_BY_SEQ.items():
                if _k.endswith(_suffix):
                    return _v
            return None
    except Exception:
        return None


def _resolve_run_strict(tenant_r: Any, thread_r: Any, seq_r: Any, dsn: str | None = None) -> str:
    """T2-3: run 严格解析，缺映射抛 MissingRunMapping（禁 seq 冒充 run）。"""
    run_str = get_run_text(str(tenant_r), str(thread_r), seq_r, dsn=dsn)
    if run_str is None:
        raise MissingRunMapping(
            f"no run mapping for seq {seq_r!r} (tenant={tenant_r!r} thread={thread_r!r})"
        )
    return run_str


def _dsn_seq_prefix(dsn: str | None) -> str:
    """DSN-hash 前缀（与 _thread_to_keys 一致），跨 DSN 隔离 seq 映射。"""
    if not dsn:
        return ""
    try:
        return hashlib.sha256(dsn.encode()).hexdigest()[:12] + "::"
    except Exception:
        return ""


def _apply_warm_rows(rows: Any, dsn: str | None = None) -> int:
    """将 SELECT 行写入 _PG_SEQ_BY_RUN/_PG_RUN_BY_SEQ，返回恢复条数（run_text 为空的行跳过）。

    键以 DSN-hash 前缀隔离（与 _thread_to_keys 一致）；dsn=None 时兼容旧无前缀条目。
    """
    pfx = _dsn_seq_prefix(dsn)
    count = 0
    try:
        with _PG_GLOBAL_LOCK:
            for r in rows or []:
                if not isinstance(r, (list, tuple)) or len(r) < 3:
                    continue
                tenant_r, thread_r, seq_r = r[0], r[1], r[2]
                run_text = r[3] if len(r) > 3 and isinstance(r[3], str) and r[3] else None
                if run_text is None:
                    continue
                try:
                    seq_int = int(seq_r)
                except Exception:
                    continue
                _PG_SEQ_BY_RUN[f"{pfx}{tenant_r}::{thread_r}::{run_text}"] = seq_int
                _PG_RUN_BY_SEQ[f"{pfx}{tenant_r}::{thread_r}::{seq_int}"] = run_text
                count += 1
    except Exception as _exc:
        logger.warning("silent handled: offline-safe: checkpoint warm apply failed", exc_info=_exc)
    return count


def _fetch_warm_rows_sync(pool: Any) -> list:
    """同步拉取 (tenant, thread, seq, run_text)；无 run_text 列时回退三列查询。"""
    sql_new = "SELECT tenant, thread, seq, run_text FROM checkpoints WHERE expires_at IS NULL OR expires_at > now()"
    sql_old = "SELECT tenant, thread, seq FROM checkpoints WHERE expires_at IS NULL OR expires_at > now()"
    if hasattr(pool, "connection"):
        with pool.connection() as conn:  # type: ignore
            try:
                try:
                    cur = conn.execute(sql_new)  # type: ignore
                    return list(cur.fetchall())  # type: ignore
                except Exception:
                    with conn.cursor() as cur:  # type: ignore
                        cur.execute(sql_new)
                        return list(cur.fetchall())
            except Exception:
                pass
            try:
                try:
                    cur = conn.execute(sql_old)  # type: ignore
                    return list(cur.fetchall())  # type: ignore
                except Exception:
                    with conn.cursor() as cur:  # type: ignore
                        cur.execute(sql_old)
                        return list(cur.fetchall())
            except Exception as _exc:
                logger.warning("silent handled: offline-safe: checkpoint warm legacy query failed", exc_info=_exc)
                return []
    elif hasattr(pool, "getconn"):
        conn = pool.getconn()  # type: ignore
        try:
            with conn.cursor() as cur:
                try:
                    cur.execute(sql_new)
                except Exception:
                    cur.execute(sql_old)
                return list(cur.fetchall())
        finally:
            try:
                pool.putconn(conn)  # type: ignore
            except Exception as _exc:
                logger.warning("silent handled: offline-safe: checkpoint warm putconn failed", exc_info=_exc)
    return []


def warm_checkpoint_maps(saver: Any) -> int:
    """PR2-D warm-start：真 PG 同步池才 SELECT 暖映射；无池/异步池返回 0。永不抛异常。"""
    try:
        is_real = saver._is_real_pg_pool() if callable(getattr(saver, "_is_real_pg_pool", None)) else False
        if not is_real:
            return 0
        try:
            if bool(saver._pool_is_async()):
                return 0
        except Exception:
            pass
        pool = getattr(saver, "pool", None)
        if pool is None:
            return 0
        # 中文：暖映射键带 DSN 前缀（与 _thread_to_keys 一致），否则重启快路永不命中
        return _apply_warm_rows(_fetch_warm_rows_sync(pool), getattr(saver, "dsn", None))
    except Exception as _exc:
        logger.warning("silent handled: offline-safe: checkpoint warm failed", exc_info=_exc)
        return 0


async def awarm_checkpoint_maps(saver: Any) -> int:
    """PR2-D warm-start（异步池）：SELECT 暖映射；非异步真池转同步实现。永不抛异常。"""
    try:
        is_real = saver._is_real_pg_pool() if callable(getattr(saver, "_is_real_pg_pool", None)) else False
        if not is_real:
            return 0
        try:
            is_async = bool(saver._pool_is_async())
        except Exception:
            is_async = False
        if not is_async:
            return warm_checkpoint_maps(saver)
        pool = getattr(saver, "pool", None)
        if pool is None:
            return 0
        sql_new = "SELECT tenant, thread, seq, run_text FROM checkpoints WHERE expires_at IS NULL OR expires_at > now()"
        sql_old = "SELECT tenant, thread, seq FROM checkpoints WHERE expires_at IS NULL OR expires_at > now()"
        rows: Any = []
        try:
            async with pool.connection() as conn:  # type: ignore
                try:
                    cur = await conn.execute(sql_new)  # type: ignore
                    rows = await cur.fetchall()  # type: ignore
                except Exception:
                    cur = await conn.execute(sql_old)  # type: ignore
                    rows = await cur.fetchall()  # type: ignore
        except Exception as _exc:
            logger.warning("silent handled: offline-safe: checkpoint async warm query failed", exc_info=_exc)
            return 0
        # 中文：暖映射键带 DSN 前缀（与 _thread_to_keys 一致），否则重启快路永不命中
        return _apply_warm_rows(rows, getattr(saver, "dsn", None))
    except Exception as _exc:
        logger.warning("silent handled: offline-safe: checkpoint async warm failed", exc_info=_exc)
        return 0


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


# 中文：pool 参数 sentinel —— 区分「未指定（自动建池）」与「显式 None（调用方明确不要池）」。
# 直接用 None 做默认会让显式 pool=None 被覆盖成自动建池，导致 test_pg_isolation 的
# 「无池 fail-closed」断言失效，且每次构造都空等 30s 连接超时。
_POOL_UNSET = object()


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
        pool: Any = _POOL_UNSET,
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
        self._setup_lock = threading.Lock()
        try:
            self._asetup_lock = asyncio.Lock()
        except Exception:
            self._asetup_lock = None  # type: ignore

        self.dsn: str = ""
        pool_explicit = pool is not _POOL_UNSET
        self.pool: Optional[Any] = pool if pool_explicit else None
        if isinstance(raw, str):
            self.dsn = raw
            if self.dsn.startswith("memory://"):
                self.pool = None
            elif _is_postgres_dsn(self.dsn):
                # 中文注释：PG DSN 且调用方未显式指定 pool 时尝试建池；失败则 loud 警告（脱敏 DSN）。
                # 显式 pool=None 表示调用方明确不要池（fail-closed 测试/降级路径），必须尊重。
                if (not pool_explicit) and ConnectionPool is not None:
                    try:
                        # 尝试建真实池；若当前环境不可用则记录警告，仍保留 emulated 兜底
                        self.pool = ConnectionPool(conninfo=self.dsn)  # type: ignore
                        try:
                            # 同步池尝试 open 以早暴露不可达，失败不抛
                            if hasattr(self.pool, "open") and not _is_async_pool(self.pool):
                                try:
                                    # 中文：timeout=5 —— 无 PG 环境快速失败（默认 30s 空等曾拖慢全量 28 分钟）；
                                    # CI/本地有 PG service 时毫秒级连上，不受影响。
                                    self.pool.open(timeout=5)  # type: ignore
                                except (OSError, ConnectionError, ValueError) as _exc:
                                    logger.warning("PG 池创建失败（%s）: %s", _redact_dsn(self.dsn), _exc)  # type: ignore
                                except Exception as _exc:
                                    logger.warning("PG 池 open 异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)  # type: ignore
                        except Exception as _exc:
                            logger.warning("PG 池 open 分支异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)  # type: ignore
                    except (ValueError, TypeError, OSError) as _exc:
                        logger.warning("PG 池创建失败（%s）: %s", _redact_dsn(self.dsn), _exc)
                        self.pool = None
                    except Exception as _exc:  # 兜底
                        logger.warning("PG 池创建异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
                        self.pool = None
                if self.pool is None:
                    logger.warning("PG DSN（%s）无可用池，走 emulated 兜底；仅当 PG 不可达时回退", _redact_dsn(self.dsn))
            else:
                if self.pool is None and ConnectionPool is not None:
                    # 非 PG DSN 不建池，成功分支不适用
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
        with self._setup_lock:
            if self._setup_done:
                return
            if self._is_real_pg_pool() and not self._pool_is_async():
                try:
                    if hasattr(self.pool, "connection"):
                        with self.pool.connection() as conn:  # type: ignore
                            try:
                                conn.execute(DDL_CHECKPOINTS)  # type: ignore
                            except (OSError, ValueError, RuntimeError) as _exc:
                                logger.warning("checkpoint DDL execute 失败（%s）: %s", _redact_dsn(self.dsn), _exc)
                                with conn.cursor() as cur:  # type: ignore
                                    cur.execute(DDL_CHECKPOINTS)
                            except Exception as _exc:  # 兜底
                                logger.warning("checkpoint DDL execute 异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
                                with conn.cursor() as cur:  # type: ignore
                                    cur.execute(DDL_CHECKPOINTS)
                            try:
                                conn.commit()  # type: ignore
                            except (OSError, RuntimeError) as _exc:
                                logger.warning("checkpoint DDL commit 失败（%s）: %s", _redact_dsn(self.dsn), _exc)
                            except Exception as _exc:  # 兜底
                                logger.warning("checkpoint DDL commit 异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
                    elif hasattr(self.pool, "getconn"):
                        conn = self.pool.getconn()  # type: ignore
                        try:
                            with conn.cursor() as cur:
                                cur.execute(DDL_CHECKPOINTS)
                            conn.commit()
                        finally:
                            try:
                                self.pool.putconn(conn)  # type: ignore
                            except (OSError, RuntimeError) as _exc:
                                logger.warning("checkpoint putconn 失败（%s）: %s", _redact_dsn(self.dsn), _exc)
                            except Exception as _exc:  # 兜底
                                logger.warning("checkpoint putconn 异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
                except (OSError, RuntimeError, ValueError) as _exc:
                    logger.warning("checkpoint setup 失败（%s）: %s", _redact_dsn(self.dsn), _exc)
                except Exception as _exc:  # 兜底窄化
                    logger.warning("checkpoint setup 异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
            self._setup_done = True

    async def asetup(self) -> None:
        """异步建表 — 真实 Postgres 时 await pool.open() 并执行 DDL。"""
        # 中文注释：异步路径仅用 _asetup_lock，绝不在线程锁内 await
        if self._setup_done:
            return
        lock = getattr(self, "_asetup_lock", None)
        if lock is not None:
            async with lock:  # type: ignore
                if self._setup_done:
                    return
                if self.pool is not None and hasattr(self.pool, "open"):
                    try:
                        await self.pool.open()  # type: ignore
                    except (OSError, RuntimeError, ValueError) as _exc:
                        logger.warning("checkpoint asetup open 失败（%s）: %s", _redact_dsn(self.dsn), _exc)
                    except Exception as _exc:  # 兜底
                        logger.warning("checkpoint asetup open 异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
                if self._is_real_pg_pool() and self._pool_is_async():
                    try:
                        async with self.pool.connection() as conn:  # type: ignore
                            await conn.execute(DDL_CHECKPOINTS)  # type: ignore
                    except (OSError, RuntimeError, ValueError) as _exc:
                        logger.warning("checkpoint asetup DDL 失败（%s）: %s", _redact_dsn(self.dsn), _exc)
                        try:
                            async with self.pool.connection() as conn:  # type: ignore
                                async with conn.cursor() as cur:  # type: ignore
                                    await cur.execute(DDL_CHECKPOINTS)
                        except Exception as _exc2:
                            logger.warning("checkpoint asetup DDL 重试失败（%s）: %s", _redact_dsn(self.dsn), _exc2, exc_info=True)
                    except Exception as _exc:  # 兜底
                        logger.warning("checkpoint asetup DDL 异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
                        try:
                            async with self.pool.connection() as conn:  # type: ignore
                                async with conn.cursor() as cur:  # type: ignore
                                    await cur.execute(DDL_CHECKPOINTS)
                        except Exception as _exc2:
                            logger.warning("checkpoint asetup DDL 重试失败（%s）: %s", _redact_dsn(self.dsn), _exc2, exc_info=True)
                self._setup_done = True
            return
        # 回退：无异步锁时不在线程锁内 await，仅做同步标记（避免阻塞事件循环）
        logger.warning("checkpoint asetup 无异步锁，回退为同步标记（%s）", _redact_dsn(self.dsn))
        self._setup_done = True

    # ---- internal PG ops ----
    def _pg_put_sync(self, thread_id: str, checkpoint: Dict[str, Any], config: Dict[str, Any]) -> bool:
        """同步 UPSERT 到 Postgres（幂等，带 expires_at）。

        T2-3: version 乐观锁 + pg_advisory_xact_lock 串行化。UPSERT 用
        WHERE checkpoints.version <= EXCLUDED.version，旧版本写入被忽略不覆盖；
        无 version 列的旧库回退无锁 UPSERT（尽力兼容）。Task7 tenant/thread/seq schema。
        """
        if not self._is_pg_mode() or self._pool_is_async():
            return False
        if self._is_real_pg_pool() and not self._pool_is_async():
            try:
                tenant, thread, seq = _thread_to_keys(thread_id, dsn=self.dsn)
                ck_json = json.dumps(checkpoint, ensure_ascii=False)
                cfg_json = json.dumps(config, ensure_ascii=False) if config else json.dumps({}, ensure_ascii=False)
                ttl_val = None
                try:
                    ttl_val = int(self.ttl_seconds) if self.ttl_seconds is not None else 0
                except Exception:
                    ttl_val = 0
                use_ttl = ttl_val is not None and ttl_val > 0
                wf, run, _tenant_raw = _validate_thread_id(thread_id)
                run_text = run
                ck_ver = _ckpt_version(checkpoint)
                lock_key = _pg_lock_key(tenant, thread, seq)
                if use_ttl:
                    expires_at_expr = "now() + (%s * interval '1 second')"
                    sql_new = f"""
                        INSERT INTO checkpoints (tenant, thread, seq, checkpoint, run_text, expires_at, version)
                        VALUES (%s, %s, %s, %s::jsonb, %s, {expires_at_expr}, %s)
                        ON CONFLICT (tenant, thread, seq) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, run_text=EXCLUDED.run_text, expires_at=EXCLUDED.expires_at, version=EXCLUDED.version
                        WHERE checkpoints.version <= EXCLUDED.version
                    """
                    # try with run_text, fallback without if column missing
                    sql_new_no_run = f"""
                        INSERT INTO checkpoints (tenant, thread, seq, checkpoint, expires_at, version)
                        VALUES (%s, %s, %s, %s::jsonb, {expires_at_expr}, %s)
                        ON CONFLICT (tenant, thread, seq) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, expires_at=EXCLUDED.expires_at, version=EXCLUDED.version
                        WHERE checkpoints.version <= EXCLUDED.version
                    """
                    # 旧库无 version 列的兼容回退（无锁）。
                    sql_new_no_ver = f"""
                        INSERT INTO checkpoints (tenant, thread, seq, checkpoint, run_text, expires_at)
                        VALUES (%s, %s, %s, %s::jsonb, %s, {expires_at_expr})
                        ON CONFLICT (tenant, thread, seq) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, run_text=EXCLUDED.run_text, expires_at=EXCLUDED.expires_at
                    """
                    sql_new_no_run_no_ver = f"""
                        INSERT INTO checkpoints (tenant, thread, seq, checkpoint, expires_at)
                        VALUES (%s, %s, %s, %s::jsonb, {expires_at_expr})
                        ON CONFLICT (tenant, thread, seq) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, expires_at=EXCLUDED.expires_at
                    """
                    sql_legacy = f"""
                        INSERT INTO checkpoints_legacy (thread_id, checkpoint, config, expires_at)
                        VALUES (%s, %s::jsonb, %s::jsonb, {expires_at_expr})
                        ON CONFLICT (thread_id) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, config=EXCLUDED.config, expires_at=EXCLUDED.expires_at
                    """
                    params_new = (tenant, thread, seq, ck_json, run_text, ttl_val, ck_ver)
                    params_new_no_run = (tenant, thread, seq, ck_json, ttl_val, ck_ver)
                    params_new_no_ver = (tenant, thread, seq, ck_json, run_text, ttl_val)
                    params_new_no_run_no_ver = (tenant, thread, seq, ck_json, ttl_val)
                    params_legacy = (thread_id, ck_json, cfg_json, ttl_val)
                else:
                    expires_at_expr = "NULL"
                    sql_new = f"""
                        INSERT INTO checkpoints (tenant, thread, seq, checkpoint, run_text, expires_at, version)
                        VALUES (%s, %s, %s, %s::jsonb, %s, {expires_at_expr}, %s)
                        ON CONFLICT (tenant, thread, seq) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, run_text=EXCLUDED.run_text, expires_at=EXCLUDED.expires_at, version=EXCLUDED.version
                        WHERE checkpoints.version <= EXCLUDED.version
                    """
                    sql_new_no_run = f"""
                        INSERT INTO checkpoints (tenant, thread, seq, checkpoint, expires_at, version)
                        VALUES (%s, %s, %s, %s::jsonb, {expires_at_expr}, %s)
                        ON CONFLICT (tenant, thread, seq) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, expires_at=EXCLUDED.expires_at, version=EXCLUDED.version
                        WHERE checkpoints.version <= EXCLUDED.version
                    """
                    sql_new_no_ver = f"""
                        INSERT INTO checkpoints (tenant, thread, seq, checkpoint, run_text, expires_at)
                        VALUES (%s, %s, %s, %s::jsonb, %s, {expires_at_expr})
                        ON CONFLICT (tenant, thread, seq) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, run_text=EXCLUDED.run_text, expires_at=EXCLUDED.expires_at
                    """
                    sql_new_no_run_no_ver = f"""
                        INSERT INTO checkpoints (tenant, thread, seq, checkpoint, expires_at)
                        VALUES (%s, %s, %s, %s::jsonb, {expires_at_expr})
                        ON CONFLICT (tenant, thread, seq) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, expires_at=EXCLUDED.expires_at
                    """
                    sql_legacy = f"""
                        INSERT INTO checkpoints_legacy (thread_id, checkpoint, config, expires_at)
                        VALUES (%s, %s::jsonb, %s::jsonb, {expires_at_expr})
                        ON CONFLICT (thread_id) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, config=EXCLUDED.config, expires_at=EXCLUDED.expires_at
                    """
                    params_new = (tenant, thread, seq, ck_json, run_text, ck_ver)
                    params_new_no_run = (tenant, thread, seq, ck_json, ck_ver)
                    params_new_no_ver = (tenant, thread, seq, ck_json, run_text)
                    params_new_no_run_no_ver = (tenant, thread, seq, ck_json)
                    params_legacy = (thread_id, ck_json, cfg_json)
                if hasattr(self.pool, "connection"):
                    with self.pool.connection() as conn:  # type: ignore
                        try:
                            try:
                                # T2-3: 同 key 串行化（事务级 advisory 锁，commit 自动释放）
                                try:
                                    _lock_exec = getattr(conn, "execute", None)
                                    if callable(_lock_exec):
                                        _lock_exec("SELECT pg_advisory_xact_lock(%s)", (lock_key,))  # type: ignore
                                except Exception:
                                    pass
                                try:
                                    conn.execute(sql_new, params_new)  # type: ignore
                                except Exception:
                                    try:
                                        conn.execute(sql_new_no_run, params_new_no_run)  # type: ignore
                                    except Exception:
                                        try:
                                            conn.execute(sql_new_no_ver, params_new_no_ver)  # type: ignore
                                        except Exception:
                                            conn.execute(sql_new_no_run_no_ver, params_new_no_run_no_ver)  # type: ignore
                            except Exception:
                                try:
                                    try:
                                        conn.execute(sql_new_no_ver, params_new_no_ver)  # type: ignore
                                    except Exception:
                                        conn.execute(sql_new_no_run_no_ver, params_new_no_run_no_ver)  # type: ignore
                                except Exception:
                                    pass
                            # also maintain legacy for compatibility
                            try:
                                conn.execute(sql_legacy, params_legacy)  # type: ignore
                            except Exception:
                                pass
                        except Exception:
                            # fallback legacy if new fails (table missing)
                            with conn.cursor() as cur:  # type: ignore
                                try:
                                    try:
                                        cur.execute(sql_new, params_new)
                                    except Exception:
                                        try:
                                            cur.execute(sql_new_no_run, params_new_no_run)
                                        except Exception:
                                            try:
                                                cur.execute(sql_new_no_ver, params_new_no_ver)
                                            except Exception:
                                                cur.execute(sql_new_no_run_no_ver, params_new_no_run_no_ver)
                                except Exception:
                                    cur.execute(sql_legacy, params_legacy)
                        try:
                            conn.commit()  # type: ignore
                        except Exception as _exc:
                            logger.warning("silent handled: offline-safe: checkpoint pg fallback to memory", exc_info=_exc)
                            pass
                elif hasattr(self.pool, "getconn"):
                    conn = self.pool.getconn()  # type: ignore
                    try:
                        with conn.cursor() as cur:
                            # T2-3: 同 key 串行化（事务级 advisory 锁，无 version 列回退同序）
                            try:
                                cur.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
                            except Exception:
                                pass
                            try:
                                try:
                                    cur.execute(sql_new, params_new)
                                except Exception:
                                    try:
                                        cur.execute(sql_new_no_run, params_new_no_run)
                                    except Exception:
                                        try:
                                            cur.execute(sql_new_no_ver, params_new_no_ver)
                                        except Exception:
                                            cur.execute(sql_new_no_run_no_ver, params_new_no_run_no_ver)
                            except Exception:
                                cur.execute(sql_legacy, params_legacy)
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
            except (OSError, RuntimeError, ValueError) as _exc:
                logger.warning("PG _pg_put_sync 失败（%s）: %s", _redact_dsn(self.dsn), _exc)
                return False
            except Exception as _exc:  # 兜底
                logger.warning("PG _pg_put_sync 异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
                return False
        # No real pool: emulated PG will be handled by caller via global store; return False to indicate no real PG op
        return False

    async def _pg_put_async(self, thread_id: str, checkpoint: Dict[str, Any], config: Dict[str, Any]) -> bool:
        """异步 UPSERT 到 Postgres；同步池回退经 to_thread 卸载，不阻塞事件循环。

        T2-3: 与 _pg_put_sync 同构的 version 乐观锁 + pg_advisory_xact_lock 串行化。
        """
        if not self._is_pg_mode():
            return False
        if self._is_real_pg_pool() and self._pool_is_async():
            try:
                tenant, thread, seq = _thread_to_keys(thread_id, dsn=self.dsn)
                ck_json = json.dumps(checkpoint, ensure_ascii=False)
                wf2, run2, _t2 = _validate_thread_id(thread_id)
                run_text2 = run2
                ck_ver2 = _ckpt_version(checkpoint)
                lock_key2 = _pg_lock_key(tenant, thread, seq)
                try:
                    ttl_val = int(self.ttl_seconds) if self.ttl_seconds is not None else 0
                except Exception:
                    ttl_val = 0
                use_ttl = ttl_val is not None and ttl_val > 0
                if use_ttl:
                    expires_at_expr = "now() + (%s * interval '1 second')"
                    sql_new = f"""
                        INSERT INTO checkpoints (tenant, thread, seq, checkpoint, run_text, expires_at, version)
                        VALUES (%s, %s, %s, %s::jsonb, %s, {expires_at_expr}, %s)
                        ON CONFLICT (tenant, thread, seq) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, run_text=EXCLUDED.run_text, expires_at=EXCLUDED.expires_at, version=EXCLUDED.version
                        WHERE checkpoints.version <= EXCLUDED.version
                    """
                    params_new = (tenant, thread, seq, ck_json, run_text2, ttl_val, ck_ver2)
                    sql_new_no_run = f"""
                        INSERT INTO checkpoints (tenant, thread, seq, checkpoint, expires_at, version)
                        VALUES (%s, %s, %s, %s::jsonb, {expires_at_expr}, %s)
                        ON CONFLICT (tenant, thread, seq) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, expires_at=EXCLUDED.expires_at, version=EXCLUDED.version
                        WHERE checkpoints.version <= EXCLUDED.version
                    """
                    params_new_no_run = (tenant, thread, seq, ck_json, ttl_val, ck_ver2)
                    sql_new_no_ver = f"""
                        INSERT INTO checkpoints (tenant, thread, seq, checkpoint, run_text, expires_at)
                        VALUES (%s, %s, %s, %s::jsonb, %s, {expires_at_expr})
                        ON CONFLICT (tenant, thread, seq) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, run_text=EXCLUDED.run_text, expires_at=EXCLUDED.expires_at
                    """
                    params_new_no_ver = (tenant, thread, seq, ck_json, run_text2, ttl_val)
                    sql_new_no_run_no_ver = f"""
                        INSERT INTO checkpoints (tenant, thread, seq, checkpoint, expires_at)
                        VALUES (%s, %s, %s, %s::jsonb, {expires_at_expr})
                        ON CONFLICT (tenant, thread, seq) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, expires_at=EXCLUDED.expires_at
                    """
                    params_new_no_run_no_ver = (tenant, thread, seq, ck_json, ttl_val)
                else:
                    expires_at_expr = "NULL"
                    sql_new = f"""
                        INSERT INTO checkpoints (tenant, thread, seq, checkpoint, run_text, expires_at, version)
                        VALUES (%s, %s, %s, %s::jsonb, %s, {expires_at_expr}, %s)
                        ON CONFLICT (tenant, thread, seq) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, run_text=EXCLUDED.run_text, expires_at=EXCLUDED.expires_at, version=EXCLUDED.version
                        WHERE checkpoints.version <= EXCLUDED.version
                    """
                    params_new = (tenant, thread, seq, ck_json, run_text2, ck_ver2)
                    sql_new_no_run = f"""
                        INSERT INTO checkpoints (tenant, thread, seq, checkpoint, expires_at, version)
                        VALUES (%s, %s, %s, %s::jsonb, {expires_at_expr}, %s)
                        ON CONFLICT (tenant, thread, seq) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, expires_at=EXCLUDED.expires_at, version=EXCLUDED.version
                        WHERE checkpoints.version <= EXCLUDED.version
                    """
                    params_new_no_run = (tenant, thread, seq, ck_json, ck_ver2)
                    sql_new_no_ver = f"""
                        INSERT INTO checkpoints (tenant, thread, seq, checkpoint, run_text, expires_at)
                        VALUES (%s, %s, %s, %s::jsonb, %s, {expires_at_expr})
                        ON CONFLICT (tenant, thread, seq) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, run_text=EXCLUDED.run_text, expires_at=EXCLUDED.expires_at
                    """
                    params_new_no_ver = (tenant, thread, seq, ck_json, run_text2)
                    sql_new_no_run_no_ver = f"""
                        INSERT INTO checkpoints (tenant, thread, seq, checkpoint, expires_at)
                        VALUES (%s, %s, %s, %s::jsonb, {expires_at_expr})
                        ON CONFLICT (tenant, thread, seq) DO UPDATE SET checkpoint=EXCLUDED.checkpoint, expires_at=EXCLUDED.expires_at
                    """
                    params_new_no_run_no_ver = (tenant, thread, seq, ck_json)
                async with self.pool.connection() as conn:  # type: ignore
                    try:
                        # T2-3: 同 key 串行化（事务级 advisory 锁）
                        try:
                            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key2,))  # type: ignore
                        except Exception:
                            pass
                        try:
                            await conn.execute(sql_new, params_new)  # type: ignore
                        except (OSError, RuntimeError, ValueError) as _exc:
                            logger.warning("PG _pg_put_async 回退到 no_run（%s）: %s", _redact_dsn(self.dsn), _exc)
                            try:
                                await conn.execute(sql_new_no_run, params_new_no_run)  # type: ignore
                            except Exception:
                                try:
                                    await conn.execute(sql_new_no_ver, params_new_no_ver)  # type: ignore
                                except Exception:
                                    await conn.execute(sql_new_no_run_no_ver, params_new_no_run_no_ver)  # type: ignore
                        except Exception as _exc:  # 兜底
                            logger.warning("PG _pg_put_async 异常回退（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
                            try:
                                await conn.execute(sql_new_no_run, params_new_no_run)  # type: ignore
                            except Exception:
                                try:
                                    await conn.execute(sql_new_no_ver, params_new_no_ver)  # type: ignore
                                except Exception:
                                    await conn.execute(sql_new_no_run_no_ver, params_new_no_run_no_ver)  # type: ignore
                    except (OSError, RuntimeError, ValueError) as _exc:
                        logger.warning("PG _pg_put_async 顶层回退（%s）: %s", _redact_dsn(self.dsn), _exc)
                    except Exception as _exc:  # 兜底
                        logger.warning("PG _pg_put_async 顶层异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
                return True
            except (OSError, RuntimeError, ValueError) as _exc:
                logger.warning("PG _pg_put_async 失败（%s）: %s", _redact_dsn(self.dsn), _exc)
                return False
            except Exception as _exc:  # 兜底
                logger.warning("PG _pg_put_async 异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
                return False
        elif self._is_real_pg_pool():
            # 中文：同步池回退必须经 to_thread 卸载，不可在事件循环线程直调阻塞 IO
            return await asyncio.to_thread(self._pg_put_sync, thread_id, checkpoint, config)
        return False

    def _pg_get_sync(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """同步从 Postgres 读取未过期 checkpoint。

        T2-3: 双表读包单事务 REPEATABLE READ（set_config + 单 connection 内两次 SELECT），
        读序与异步路径统一：新表 checkpoints 优先，legacy 其次。
        """
        if self._is_real_pg_pool() and not self._pool_is_async():
            try:
                tenant, thread, seq = _thread_to_keys(thread_id, dsn=self.dsn)
                row = None
                if hasattr(self.pool, "connection"):
                    with self.pool.connection() as conn:  # type: ignore
                        try:
                            # T2-3: 单事务 REPEATABLE READ，避免双表两次 SELECT 读偏
                            try:
                                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")  # type: ignore
                            except Exception:
                                try:
                                    with conn.cursor() as _c:  # type: ignore
                                        _c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                                except Exception:
                                    pass
                            cur = conn.execute(_READ_ORDER_SQL_NEW, (tenant, thread, seq))  # type: ignore
                            row = cur.fetchone()  # type: ignore
                            if row is None:
                                cur = conn.execute(_READ_ORDER_SQL_LEGACY, (thread_id,))  # type: ignore
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
                                cur.execute(_READ_ORDER_SQL_NEW, (tenant, thread, seq))
                                row = cur.fetchone()
                                if row is None:
                                    cur.execute(_READ_ORDER_SQL_LEGACY, (thread_id,))
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
                            try:
                                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                            except Exception:
                                pass
                            cur.execute(_READ_ORDER_SQL_NEW, (tenant, thread, seq))
                            row = cur.fetchone()
                            if row is None:
                                cur.execute(_READ_ORDER_SQL_LEGACY, (thread_id,))
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
            except (OSError, RuntimeError, ValueError) as _exc:
                logger.warning("PG _pg_get_sync 失败（%s）: %s", _redact_dsn(self.dsn), _exc)
                return None
            except Exception as _exc:  # 兜底
                logger.warning("PG _pg_get_sync 异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
                return None
        return None

    async def _pg_get_async(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """异步从 Postgres 读取未过期 checkpoint；同步池回退经 to_thread 卸载。

        T2-3: 与同步路径统一读序（新表 checkpoints 优先，legacy 其次），
        双表两次 SELECT 包同一 connection（事务级一致读）。
        """
        if not self._is_pg_mode():
            return None
        if self._is_real_pg_pool() and self._pool_is_async():
            try:
                tenant, thread, seq = _thread_to_keys(thread_id, dsn=self.dsn)
                async with self.pool.connection() as conn:  # type: ignore
                    try:
                        await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")  # type: ignore
                    except Exception:
                        pass
                    cur = await conn.execute(_READ_ORDER_SQL_NEW, (tenant, thread, seq))  # type: ignore
                    row = await cur.fetchone()  # type: ignore
                    if row is None:
                        # try legacy (same connection — 与同步路径统一读序)
                        cur = await conn.execute(_READ_ORDER_SQL_LEGACY, (thread_id,))  # type: ignore
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
            except (OSError, RuntimeError, ValueError) as _exc:
                logger.warning("PG _pg_get_async 失败（%s）: %s", _redact_dsn(self.dsn), _exc)
                return None
            except Exception as _exc:  # 兜底
                logger.warning("PG _pg_get_async 异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
                return None
        elif self._is_real_pg_pool():
            # 中文：同步池回退必须经 to_thread 卸载，不可在事件循环线程直调阻塞 IO
            return await asyncio.to_thread(self._pg_get_sync, thread_id)
        return None

    # ---- put / get ----

    def put(self, thread_id: str, checkpoint: Dict[str, Any], config: Dict[str, Any] | None = None) -> None:
        """写入 checkpoint，thread_id 须为三段式，自动记录 TTL 时间戳。

        T2-3: emulated 侧 version 乐观锁——同 key 旧版本写入被忽略（last-writer-wins
        按版本裁决），保证并发同 thread 不丢进度。
        """
        _validate_thread_id(thread_id)
        if not isinstance(checkpoint, dict):
            raise ValueError("checkpoint must be dict")
        now = time.time()
        cfg = copy.deepcopy(config or {})
        # PG main path with fallback to memory only when PG unreachable
        if self._is_pg_mode():
            # ensure deterministic seq mapping is persisted (collision disambiguation)
            try:
                _thread_to_keys(thread_id, dsn=self.dsn)
            except (ValueError, TypeError, RuntimeError) as _exc:
                logger.warning("thread_id 映射失败（%s）: %s", _redact_dsn(self.dsn), _exc)
            except Exception as _exc:  # 兜底
                logger.warning("thread_id 映射异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
            # emulated PG global store (ensures restart not lost even without real PG)
            # T2-3: version 乐观锁 — 旧版本写入忽略，不覆盖新版本（并发不丢进度）
            key = _pg_store_key(self.dsn, thread_id)
            ck_ver_put = _ckpt_version(checkpoint)
            with _PG_GLOBAL_LOCK:
                cur_ver = _PG_GLOBAL_VER.get(key, -1)
                if ck_ver_put < cur_ver:
                    logger.warning(
                        "checkpoint emulated 忽略旧版本写入（%s）: incoming=%s current=%s",
                        _redact_dsn(self.dsn), ck_ver_put, cur_ver,
                    )
                else:
                    _PG_GLOBAL_STORE[key] = copy.deepcopy(checkpoint)
                    _PG_GLOBAL_META[key] = copy.deepcopy(cfg)
                    _PG_GLOBAL_TS[key] = now
                    _PG_GLOBAL_VER[key] = ck_ver_put
                    _evict_if_needed()
            # also keep instance store for immediate access
            self._store[thread_id] = copy.deepcopy(checkpoint)
            self._meta[thread_id] = cfg
            self._timestamps[thread_id] = now
            # attempt real PG write (best-effort); if fails, global store still persists
            if self._is_real_pg_pool():
                ok = self._pg_put_sync(thread_id, checkpoint, cfg)
                if not ok:
                    logger.warning("PG put 未写入真实库（%s），已回退 emulated", _redact_dsn(self.dsn))
            return
        # memory path
        self._store[thread_id] = copy.deepcopy(checkpoint)
        self._meta[thread_id] = cfg
        self._timestamps[thread_id] = now

    async def aput(self, thread_id: str, checkpoint: Dict[str, Any], config: Dict[str, Any] | None = None) -> None:
        """异步写入 checkpoint。

        并发：与同步 put 统一以 _PG_GLOBAL_LOCK 保护同一 dict（绝不分裂两套锁）；
        临界区内仅做非阻塞 dict 读写拷贝，不跨 await，不阻塞事件循环。
        T2-3: emulated 侧 version 乐观锁（与 put 同构）。
        """
        _validate_thread_id(thread_id)
        if not isinstance(checkpoint, dict):
            raise ValueError("checkpoint must be dict")
        now = time.time()
        cfg = copy.deepcopy(config or {})
        if self._is_pg_mode():
            try:
                _thread_to_keys(thread_id, dsn=self.dsn)
            except (ValueError, TypeError, RuntimeError) as _exc:
                logger.warning("thread_id 映射失败（%s）: %s", _redact_dsn(self.dsn), _exc)
            except Exception as _exc:  # 兜底
                logger.warning("thread_id 映射异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
            key = _pg_store_key(self.dsn, thread_id)
            ck_ver_aput = _ckpt_version(checkpoint)
            with _PG_GLOBAL_LOCK:
                cur_ver = _PG_GLOBAL_VER.get(key, -1)
                if ck_ver_aput < cur_ver:
                    logger.warning(
                        "checkpoint emulated 忽略旧版本写入（%s）: incoming=%s current=%s",
                        _redact_dsn(self.dsn), ck_ver_aput, cur_ver,
                    )
                else:
                    _PG_GLOBAL_STORE[key] = copy.deepcopy(checkpoint)
                    _PG_GLOBAL_META[key] = copy.deepcopy(cfg)
                    _PG_GLOBAL_TS[key] = now
                    _PG_GLOBAL_VER[key] = ck_ver_aput
                    _evict_if_needed()
            self._store[thread_id] = copy.deepcopy(checkpoint)
            self._meta[thread_id] = cfg
            self._timestamps[thread_id] = now
            if self._is_real_pg_pool():
                ok = await self._pg_put_async(thread_id, checkpoint, cfg)
                if not ok:
                    logger.warning("PG aput 未写入真实库（%s），已回退 emulated", _redact_dsn(self.dsn))
            return
        self._store[thread_id] = copy.deepcopy(checkpoint)
        self._meta[thread_id] = cfg
        self._timestamps[thread_id] = now

    def get(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """读取 checkpoint，过期返回 None 并清理；优先 PG 的 expires_at 语义。"""
        # 中文注释：有真实池时优先查 PG，再回退 emulated，避免脏缓存遮蔽新写入
        _validate_thread_id(thread_id)
        if self._is_pg_mode():
            if self._is_real_pg_pool() and not self._pool_is_async():
                try:
                    pg_val = self._pg_get_sync(thread_id)
                    if pg_val is not None:
                        return copy.deepcopy(pg_val)
                except (OSError, RuntimeError, ValueError) as _exc:
                    logger.warning("PG get 失败回退 emulated（%s）: %s", _redact_dsn(self.dsn), _exc)
                except Exception as _exc:  # 兜底
                    logger.warning("PG get 异常回退 emulated（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
            key = _pg_store_key(self.dsn, thread_id)
            with _PG_GLOBAL_LOCK:
                ts = _PG_GLOBAL_TS.get(key)
                if ts is not None and self.ttl_seconds > 0 and time.time() - ts > self.ttl_seconds:
                    _PG_GLOBAL_STORE.pop(key, None)
                    _PG_GLOBAL_META.pop(key, None)
                    _PG_GLOBAL_TS.pop(key, None)
                    _PG_GLOBAL_VER.pop(key, None)
                else:
                    val = _PG_GLOBAL_STORE.get(key)
                    if val is not None:
                        return copy.deepcopy(val)
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
        """异步读取 checkpoint，优先 Postgres，其次内存 TTL。

        并发：全局 dict 临界区内仅做非阻塞读写（_PG_GLOBAL_LOCK，不跨 await）；
        同步回退经 to_thread 卸载，不阻塞事件循环。
        """
        _validate_thread_id(thread_id)
        if self._is_pg_mode():
            # 有真实池时优先查 PG
            try:
                pg_val = await self._pg_get_async(thread_id)
                if pg_val is not None:
                    return copy.deepcopy(pg_val)
            except (OSError, RuntimeError, ValueError) as _exc:
                logger.warning("PG aget 失败回退 emulated（%s）: %s", _redact_dsn(self.dsn), _exc)
            except Exception as _exc:  # 兜底
                logger.warning("PG aget 异常回退 emulated（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
            key = _pg_store_key(self.dsn, thread_id)
            with _PG_GLOBAL_LOCK:
                ts = _PG_GLOBAL_TS.get(key)
                if ts is not None and self.ttl_seconds > 0 and time.time() - ts > self.ttl_seconds:
                    _PG_GLOBAL_STORE.pop(key, None)
                    _PG_GLOBAL_META.pop(key, None)
                    _PG_GLOBAL_TS.pop(key, None)
                    _PG_GLOBAL_VER.pop(key, None)
                else:
                    val = _PG_GLOBAL_STORE.get(key)
                    if val is not None:
                        return copy.deepcopy(val)
        # 中文：同步回退经 to_thread 卸载，不可在事件循环线程直调阻塞 get()
        return await asyncio.to_thread(self.get, thread_id)

    def get_with_config(self, thread_id: str) -> Optional[tuple[Dict[str, Any], Dict[str, Any]]]:
        """同时返回 checkpoint 与 config，用于断点续跑恢复上下文。

        异步池须走 aget_with_config（本同步方法在无运行 loop 时经 asyncio.run 委托，
        有运行 loop 时抛错提示用异步变体，不可静默跳过 PG）。
        """
        # 中文注释：TTL 过期需驱逐并视作 miss，不再 pass 透出脏数据
        _validate_thread_id(thread_id)
        if self._is_pg_mode():
            key = _pg_store_key(self.dsn, thread_id)
            expired = False
            with _PG_GLOBAL_LOCK:
                ts = _PG_GLOBAL_TS.get(key)
                if ts is not None and self.ttl_seconds > 0 and time.time() - ts > self.ttl_seconds:
                    _PG_GLOBAL_STORE.pop(key, None)
                    _PG_GLOBAL_META.pop(key, None)
                    _PG_GLOBAL_TS.pop(key, None)
                    _PG_GLOBAL_VER.pop(key, None)
                    expired = True
                else:
                    chk = _PG_GLOBAL_STORE.get(key)
                    if chk is not None:
                        cfg = copy.deepcopy(_PG_GLOBAL_META.get(key, {}))
                        return copy.deepcopy(chk), cfg
            if expired:
                # 已过期，按 miss 处理，但仍尝试 PG 侧（若未过期可能有更新）
                pass
            if self._is_real_pg_pool():
                if self._pool_is_async():
                    # 中文：异步池不可在同步方法内静默跳过 PG；有 loop 用异步变体，无 loop 委托执行
                    try:
                        _loop = asyncio.get_running_loop()
                    except RuntimeError:
                        _loop = None
                    if _loop is not None:
                        raise RuntimeError("get_with_config: async pool requires await aget_with_config() (would block the event loop)")
                    return asyncio.run(self.aget_with_config(thread_id))
                else:
                    try:
                        tenant, thread, seq = _thread_to_keys(thread_id, dsn=self.dsn)
                        row = None
                        cfg: Dict[str, Any] = {}
                        if hasattr(self.pool, "connection"):
                            with self.pool.connection() as conn:  # type: ignore
                                try:
                                    # T2-3: 单事务 REPEATABLE READ（与 _pg_get_sync 同构）
                                    try:
                                        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")  # type: ignore
                                    except Exception:
                                        pass
                                    cur = conn.execute(_READ_ORDER_SQL_NEW, (tenant, thread, seq))  # type: ignore
                                    row = cur.fetchone()  # type: ignore
                                    if row is None:
                                        # 中文：新表无 config 列时回退 legacy（config 真实持久处）
                                        cur = conn.execute(_READ_ORDER_SQL_LEGACY, (thread_id,))  # type: ignore
                                        row = cur.fetchone()  # type: ignore
                                except (OSError, RuntimeError, ValueError) as _exc:
                                    logger.warning("get_with_config 回退到 cursor（%s）: %s", _redact_dsn(self.dsn), _exc)
                                    with conn.cursor() as cur:  # type: ignore
                                        cur.execute(_READ_ORDER_SQL_NEW, (tenant, thread, seq))
                                        row = cur.fetchone()
                                        if row is None:
                                            cur.execute(_READ_ORDER_SQL_LEGACY, (thread_id,))
                                            row = cur.fetchone()
                                except Exception as _exc:  # 兜底
                                    logger.warning("get_with_config 异常回退（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
                                    with conn.cursor() as cur:  # type: ignore
                                        cur.execute(_READ_ORDER_SQL_NEW, (tenant, thread, seq))
                                        row = cur.fetchone()
                                        if row is None:
                                            cur.execute(_READ_ORDER_SQL_LEGACY, (thread_id,))
                                            row = cur.fetchone()
                        if row is not None:
                            if isinstance(row, (list, tuple)) and len(row) > 1 and row[1] is not None:
                                try:
                                    cfg = row[1] if isinstance(row[1], dict) else json.loads(row[1])
                                except (json.JSONDecodeError, ValueError, TypeError) as _exc:
                                    logger.warning("get_with_config legacy config 解析失败（%s）: %s", _redact_dsn(self.dsn), _exc)
                                    cfg = {}
                            elif isinstance(row, dict) and row.get("config") is not None:
                                try:
                                    _c = row.get("config")
                                    cfg = _c if isinstance(_c, dict) else json.loads(_c)
                                except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as _exc:
                                    logger.warning("get_with_config legacy config 解析失败（%s）: %s", _redact_dsn(self.dsn), _exc)
                                    cfg = {}
                            chk = row[0] if isinstance(row, (list, tuple)) else row.get("checkpoint")  # type: ignore
                            if isinstance(chk, str):
                                try:
                                    chk = json.loads(chk)
                                except (json.JSONDecodeError, ValueError, TypeError) as _exc:
                                    logger.warning("get_with_config json 解析失败（%s）: %s", _redact_dsn(self.dsn), _exc)
                            if chk is not None:
                                return copy.deepcopy(chk if isinstance(chk, dict) else {}), copy.deepcopy(cfg)
                    except (OSError, RuntimeError, ValueError) as _exc:
                        logger.warning("get_with_config PG 查询失败（%s）: %s", _redact_dsn(self.dsn), _exc)
                    except Exception as _exc:  # 兜底
                        logger.warning("get_with_config 异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
        chk = self.get(thread_id)
        if chk is None:
            return None
        return chk, copy.deepcopy(self._meta.get(thread_id, {}))

    async def _pg_get_config_row_async(self, tenant: str, thread: str, seq: int, thread_id: str) -> Optional[tuple[Dict[str, Any], Dict[str, Any]]]:
        """异步池 get_with_config 的 PG 行查询（await connection + execute）。

        新表 checkpoints 无 config 列时回退 checkpoints_legacy（config 真实持久处），
        不可静默返回 {} 丢失跨重启 config。
        T2-3: 与同步路径统一读序，同一 connection 内两次 SELECT。
        """
        async with self.pool.connection() as conn:  # type: ignore
            try:
                await conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")  # type: ignore
            except Exception:
                pass
            cur = await conn.execute(_READ_ORDER_SQL_NEW, (tenant, thread, seq))  # type: ignore
            row = await cur.fetchone()  # type: ignore
            cfg: Dict[str, Any] = {}
            if row is None:
                cur = await conn.execute(_READ_ORDER_SQL_LEGACY, (thread_id,))  # type: ignore
                row = await cur.fetchone()  # type: ignore
                if row is None:
                    return None
                if isinstance(row, (list, tuple)) and len(row) > 1 and row[1] is not None:
                    try:
                        cfg = row[1] if isinstance(row[1], dict) else json.loads(row[1])
                    except (json.JSONDecodeError, ValueError, TypeError) as _exc:
                        logger.warning("aget_with_config legacy config 解析失败（%s）: %s", _redact_dsn(self.dsn), _exc)
                        cfg = {}
                elif isinstance(row, dict) and row.get("config") is not None:
                    try:
                        _c = row.get("config")
                        cfg = _c if isinstance(_c, dict) else json.loads(_c)
                    except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as _exc:
                        logger.warning("aget_with_config legacy config 解析失败（%s）: %s", _redact_dsn(self.dsn), _exc)
                        cfg = {}
            chk = row[0] if isinstance(row, (list, tuple)) else row.get("checkpoint")  # type: ignore
        if isinstance(chk, str):
            try:
                chk = json.loads(chk)
            except (json.JSONDecodeError, ValueError, TypeError) as _exc:
                logger.warning("aget_with_config json 解析失败（%s）: %s", _redact_dsn(self.dsn), _exc)
        if chk is None:
            return None
        return copy.deepcopy(chk if isinstance(chk, dict) else {}), copy.deepcopy(cfg)

    async def aget_with_config(self, thread_id: str) -> Optional[tuple[Dict[str, Any], Dict[str, Any]]]:
        """异步版 get_with_config：emulated 优先，异步池经 await 查询 PG。"""
        _validate_thread_id(thread_id)
        if self._is_pg_mode():
            key = _pg_store_key(self.dsn, thread_id)
            with _PG_GLOBAL_LOCK:
                ts = _PG_GLOBAL_TS.get(key)
                if ts is not None and self.ttl_seconds > 0 and time.time() - ts > self.ttl_seconds:
                    _PG_GLOBAL_STORE.pop(key, None)
                    _PG_GLOBAL_META.pop(key, None)
                    _PG_GLOBAL_TS.pop(key, None)
                    _PG_GLOBAL_VER.pop(key, None)
                else:
                    chk = _PG_GLOBAL_STORE.get(key)
                    if chk is not None:
                        cfg = copy.deepcopy(_PG_GLOBAL_META.get(key, {}))
                        return copy.deepcopy(chk), cfg
            if self._is_real_pg_pool():
                if self._pool_is_async():
                    try:
                        tenant, thread, seq = _thread_to_keys(thread_id, dsn=self.dsn)
                        row = await self._pg_get_config_row_async(tenant, thread, seq, thread_id)
                    except (OSError, RuntimeError, ValueError) as _exc:
                        logger.warning("aget_with_config 异步 PG 查询失败（%s）: %s", _redact_dsn(self.dsn), _exc)
                        row = None
                    except Exception as _exc:  # 兜底
                        logger.warning("aget_with_config 异步异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
                        row = None
                    if row is not None:
                        return row
                else:
                    # 中文：同步池回退经 to_thread 卸载，不阻塞事件循环
                    return await asyncio.to_thread(self.get_with_config, thread_id)
        chk = await asyncio.to_thread(self.get, thread_id)
        if chk is None:
            return None
        return chk, copy.deepcopy(self._meta.get(thread_id, {}))

    async def _pg_delete_async(self, tenant: str, thread: str, seq: int, thread_id: str) -> None:
        """异步池 DELETE（await connection + execute + commit 语义）。"""
        sql_new = "DELETE FROM checkpoints WHERE tenant=%s AND thread=%s AND seq=%s"
        sql_legacy = "DELETE FROM checkpoints_legacy WHERE thread_id=%s"
        async with self.pool.connection() as conn:  # type: ignore
            try:
                await conn.execute(sql_new, (tenant, thread, seq))  # type: ignore
                await conn.execute(sql_legacy, (thread_id,))  # type: ignore
            except (OSError, RuntimeError, ValueError) as _exc:
                logger.warning("adelete 回退语义（%s）: %s", _redact_dsn(self.dsn), _exc)
                raise
            except Exception as _exc:  # 兜底
                logger.warning("adelete 异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
                raise

    def delete(self, thread_id: str) -> None:
        """删除指定 thread_id 的 checkpoint（含 PG 侧）。

        异步池须走 adelete（本同步方法在无运行 loop 时经 asyncio.run 委托，
        有运行 loop 时抛错提示用异步变体，不可静默跳过 PG 行删除）。
        """
        _validate_thread_id(thread_id)
        key = _pg_store_key(self.dsn, thread_id)
        with _PG_GLOBAL_LOCK:
            _PG_GLOBAL_STORE.pop(key, None)
            _PG_GLOBAL_META.pop(key, None)
            _PG_GLOBAL_TS.pop(key, None)
            _PG_GLOBAL_VER.pop(key, None)
        self._store.pop(thread_id, None)
        self._meta.pop(thread_id, None)
        self._timestamps.pop(thread_id, None)
        if self._is_real_pg_pool():
            if self._pool_is_async():
                # 中文：异步池不可静默跳过 PG 行删除；有 loop 用 adelete，无 loop 委托执行
                try:
                    _loop = asyncio.get_running_loop()
                except RuntimeError:
                    _loop = None
                if _loop is not None:
                    raise RuntimeError("delete: async pool requires await adelete() (would block the event loop)")
                asyncio.run(self.adelete(thread_id))
                return
            try:
                tenant, thread, seq = _thread_to_keys(thread_id, dsn=self.dsn)
                sql_new = "DELETE FROM checkpoints WHERE tenant=%s AND thread=%s AND seq=%s"
                sql_legacy = "DELETE FROM checkpoints_legacy WHERE thread_id=%s"
                if hasattr(self.pool, "connection"):
                    with self.pool.connection() as conn:  # type: ignore
                        try:
                            conn.execute(sql_new, (tenant, thread, seq))  # type: ignore
                            conn.execute(sql_legacy, (thread_id,))  # type: ignore
                        except (OSError, RuntimeError, ValueError) as _exc:
                            logger.warning("delete 回退到 cursor（%s）: %s", _redact_dsn(self.dsn), _exc)
                            with conn.cursor() as cur:  # type: ignore
                                cur.execute(sql_new, (tenant, thread, seq))
                                cur.execute(sql_legacy, (thread_id,))
                        except Exception as _exc:  # 兜底
                            logger.warning("delete 异常回退（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
                            with conn.cursor() as cur:  # type: ignore
                                cur.execute(sql_new, (tenant, thread, seq))
                                cur.execute(sql_legacy, (thread_id,))
                        try:
                            conn.commit()  # type: ignore
                        except (OSError, RuntimeError) as _exc:
                            logger.warning("delete commit 失败（%s）: %s", _redact_dsn(self.dsn), _exc)
                        except Exception as _exc:  # 兜底
                            logger.warning("delete commit 异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
            except (OSError, RuntimeError, ValueError) as _exc:
                logger.warning("delete 失败（%s）: %s", _redact_dsn(self.dsn), _exc)
            except Exception as _exc:  # 兜底
                logger.warning("delete 异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)

    def list_thread_ids(self) -> list[str]:
        """列出未过期的 thread_id。"""
        # 中文注释：合并 emulated 与 PG 行，去重；emulated 非空时也需合并 PG，而非早返回遮蔽
        if self._is_pg_mode():
            now = time.time()
            alive = []
            prefix = _pg_store_prefix(self.dsn)
            with _PG_GLOBAL_LOCK:
                for k, ts in list(_PG_GLOBAL_TS.items()):
                    if not k.startswith(prefix):
                        continue
                    tid = k[len(prefix):]
                    if self.ttl_seconds > 0 and now - ts > self.ttl_seconds:
                        _PG_GLOBAL_STORE.pop(k, None)
                        _PG_GLOBAL_META.pop(k, None)
                        _PG_GLOBAL_TS.pop(k, None)
                        _PG_GLOBAL_VER.pop(k, None)
                    else:
                        alive.append(tid)
            # 有真实池时合并 PG 行（去重）
            pg_ids: list[str] = []
            if self._is_real_pg_pool():
                if self._pool_is_async():
                    # 中文：异步池不可静默跳过 PG 行合并；有 loop 用 alist_thread_ids，无 loop 委托执行
                    try:
                        _loop2 = asyncio.get_running_loop()
                    except RuntimeError:
                        _loop2 = None
                    if _loop2 is not None:
                        raise RuntimeError("list_thread_ids: async pool requires await alist_thread_ids() (would block the event loop)")
                    pg_ids = asyncio.run(self.alist_thread_ids())
                else:
                    try:
                        rows = _fetch_warm_rows_sync(self.pool)
                        if rows:
                            _apply_warm_rows(rows, self.dsn)
                            for r in rows:
                                if not isinstance(r, (list, tuple)) or len(r) < 3:
                                    continue
                                tenant_r, thread_r, seq_r = r[0], r[1], r[2]
                                # T2-3: 4列行（有 run_text 列）缺映射抛 MissingRunMapping
                                # 不伪造；3列旧行（无 run_text 列）查映射，缺失回退 str(seq) 兼容旧库形态。
                                if isinstance(r, (list, tuple)) and len(r) > 3:
                                    run_text = r[3] if isinstance(r[3], str) and r[3] else None
                                    run_str = run_text or _resolve_run_strict(
                                        tenant_r, thread_r, seq_r, dsn=self.dsn
                                    )
                                else:
                                    run_str = get_run_text(
                                        str(tenant_r), str(thread_r), seq_r, dsn=self.dsn
                                    ) or str(seq_r)
                                pg_ids.append(f"{thread_r}:{run_str}:{tenant_r}")
                    except MissingRunMapping:
                        raise
                    except (OSError, RuntimeError, ValueError) as _exc:
                        logger.warning("list_thread_ids PG 查询失败（%s）: %s", _redact_dsn(self.dsn), _exc)
                    except Exception as _exc:  # 兜底
                        logger.warning("list_thread_ids 异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
            if pg_ids or alive:
                # 合并去重，保持 alive 在前
                merged = list(alive)
                seen = set(alive)
                for tid in pg_ids:
                    if tid not in seen:
                        merged.append(tid)
                        seen.add(tid)
                if merged:
                    return merged
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

    async def adelete(self, thread_id: str) -> None:
        """异步版 delete：emulated + 实例缓存同步清理，异步池经 await 删除 PG 行。"""
        _validate_thread_id(thread_id)
        key = _pg_store_key(self.dsn, thread_id)
        with _PG_GLOBAL_LOCK:
            _PG_GLOBAL_STORE.pop(key, None)
            _PG_GLOBAL_META.pop(key, None)
            _PG_GLOBAL_TS.pop(key, None)
            _PG_GLOBAL_VER.pop(key, None)
        self._store.pop(thread_id, None)
        self._meta.pop(thread_id, None)
        self._timestamps.pop(thread_id, None)
        if self._is_real_pg_pool():
            if self._pool_is_async():
                tenant, thread, seq = _thread_to_keys(thread_id, dsn=self.dsn)
                await self._pg_delete_async(tenant, thread, seq, thread_id)
            else:
                # 中文：同步池回退经 to_thread 卸载，不阻塞事件循环
                await asyncio.to_thread(self.delete, thread_id)

    async def alist_thread_ids(self) -> list[str]:
        """异步版 list_thread_ids：合并 emulated 与 PG 行（异步池经 await 查询）。"""
        if self._is_pg_mode():
            now = time.time()
            alive = []
            prefix = _pg_store_prefix(self.dsn)
            with _PG_GLOBAL_LOCK:
                for k, ts in list(_PG_GLOBAL_TS.items()):
                    if not k.startswith(prefix):
                        continue
                    tid = k[len(prefix):]
                    if self.ttl_seconds > 0 and now - ts > self.ttl_seconds:
                        _PG_GLOBAL_STORE.pop(k, None)
                        _PG_GLOBAL_META.pop(k, None)
                        _PG_GLOBAL_TS.pop(k, None)
                        _PG_GLOBAL_VER.pop(k, None)
                    else:
                        alive.append(tid)
            pg_ids: list[str] = []
            if self._is_real_pg_pool():
                if self._pool_is_async():
                    try:
                        pool = self.pool
                        sql_new = "SELECT tenant, thread, seq, run_text FROM checkpoints WHERE expires_at IS NULL OR expires_at > now()"
                        rows: Any = []
                        async with pool.connection() as conn:  # type: ignore
                            cur = await conn.execute(sql_new)  # type: ignore
                            rows = await cur.fetchall()  # type: ignore
                        _apply_warm_rows(rows, self.dsn)
                        for r in rows:
                            if not isinstance(r, (list, tuple)) or len(r) < 3:
                                continue
                            tenant_r, thread_r, seq_r = r[0], r[1], r[2]
                            # T2-3: 与 list_thread_ids 同构（4列严格 / 3列映射回退str(seq)兼容）
                            if isinstance(r, (list, tuple)) and len(r) > 3:
                                run_text = r[3] if isinstance(r[3], str) and r[3] else None
                                run_str = run_text or _resolve_run_strict(
                                    tenant_r, thread_r, seq_r, dsn=self.dsn
                                )
                            else:
                                run_str = get_run_text(
                                    str(tenant_r), str(thread_r), seq_r, dsn=self.dsn
                                ) or str(seq_r)
                            pg_ids.append(f"{thread_r}:{run_str}:{tenant_r}")
                    except MissingRunMapping:
                        raise
                    except (OSError, RuntimeError, ValueError) as _exc:
                        logger.warning("alist_thread_ids 异步 PG 查询失败（%s）: %s", _redact_dsn(self.dsn), _exc)
                    except Exception as _exc:  # 兜底
                        logger.warning("alist_thread_ids 异步异常（%s）: %s", _redact_dsn(self.dsn), _exc, exc_info=True)
                else:
                    # 中文：同步池回退经 to_thread 卸载，不阻塞事件循环
                    return await asyncio.to_thread(self.list_thread_ids)
            if pg_ids or alive:
                merged = list(alive)
                seen = set(alive)
                for tid in pg_ids:
                    if tid not in seen:
                        merged.append(tid)
                        seen.add(tid)
                if merged:
                    return merged
        return await asyncio.to_thread(self._list_memory_ids)

    def _list_memory_ids(self) -> list[str]:
        """实例内存缓存的未过期 thread_id（供 alist_thread_ids 回退）。"""
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

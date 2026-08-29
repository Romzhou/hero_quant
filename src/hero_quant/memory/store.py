"""记忆存储：文件 + SQLite FTS5 双存储与向量混合检索。

职责：对外提供记忆的写入、检索与去重；上游供 Agent/Graph 调用，下游落盘到文件、SQLite 索引及可选 pgvector。
设计要点：文件为真实来源（可审计、支持层次路由），SQLite FTS5 为检索索引（BM25）；优先 trigram 分词以兼顾中英文；30 秒滑动窗口内对归一化 content 去重；向量检索本地优先、pgvector sidecar 配置时作为一等公民且失败静默回退。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .lifecycle import compute_importance

logger = logging.getLogger("hero_quant.memory.store")


class _SQLiteConnWrapper:
    """Wrapper around sqlite3.Connection to allow commit patching in tests (Python 3.13 commit is read-only)."""

    def __init__(self, conn: sqlite3.Connection):
        object.__setattr__(self, "_real", conn)

    def __getattr__(self, name: str):
        return getattr(self._real, name)

    def __setattr__(self, name: str, value):
        if name == "_real":
            object.__setattr__(self, name, value)
        else:
            self.__dict__[name] = value


def _content_hash(name: str, content: str) -> str:
    """计算去重哈希：对 ``name:content`` 归一化后取 sha256 前 16 位。"""
    return hashlib.sha256(f"{name}:{content}".lower().strip().encode()).hexdigest()[:16]


def _content_bigrams(content: str) -> str:
    """将内容预切为相邻二字 token，供无 trigram 时的 FTS5 回退使用。"""
    return " ".join(
        content[index : index + 2]
        for index in range(len(content) - 1)
        if not any(char.isspace() for char in content[index : index + 2])
    )


# pgvector sidecar 的 DSN 前缀白名单；鉴权与解析统一收口到 Settings，此处仅做轻量委托。
_PG_PREFIXES = ("postgresql://", "postgres://", "postgresql+psycopg://")


def _pgvector_dsn() -> str | None:
    """经 Settings 解析 pgvector DSN，未配置或格式非法则返回 None。"""
    try:
        from hero_quant.config.settings import Settings

        s = Settings()
        dsn = s.vector_dsn
        if dsn and isinstance(dsn, str) and dsn.strip().startswith(_PG_PREFIXES):
            return dsn.strip()
        return None
    except Exception:
        return None


def is_pgvector_configured() -> bool:
    """判断是否应启用 pgvector sidecar（由 Settings 统一鉴权）。"""
    try:
        from hero_quant.config.settings import Settings

        s = Settings()
        store = (s.vector_store or "").strip().lower() if s.vector_store else ""
        if store in ("local", "sqlite", "memory", "none", "offline", "disable", "disabled"):
            return False
        return _pgvector_dsn() is not None
    except Exception:
        return False


def get_vector_dim() -> int:
    """获取向量维度，单一来源委托给 ``hero_quant.agent.embed``。"""
    try:
        from hero_quant.agent.embed import get_vector_dim as _embed_dim

        return _embed_dim()
    except Exception:
        return 32


def _vector_to_literal(vec) -> str:
    """将向量序列化为 pgvector 字面量 ``[0.1,0.2]``，兼顾 vector 类型与 TEXT 回退。"""
    # finite check first — must raise even if delegate would silently format inf/nan
    for x in vec:
        fv = float(x)
        if not math.isfinite(fv):
            raise ValueError(f"non-finite vector value {x!r}")
    try:
        from hero_quant.agent.embed import to_pgvector_literal  # type: ignore

        return to_pgvector_literal(vec)  # type: ignore
    except Exception:
        pass
    vals: list[str] = []
    for x in vec:
        vals.append(f"{float(x):.6f}")
    return "[" + ",".join(vals) + "]"


class PgVectorSidecar:
    """Postgres pgvector 侧车：配置时一等公民，未就绪则静默回退到本地。

    职责：承载向量的一致性扩容，状态由 ``_enabled/_pool/_is_async`` 控制，连接失败时所有操作 no-op。
    不变量：DDL 幂等（vector 扩展 + memory_vectors 表 + 索引），UPSERT 按 key 冲突覆盖，检索按余弦距离 ``<=>`` 排序并支持 namespace 过滤；超时统一 5s 熔断。
    """

    def __init__(self, dsn: str | None = None, dim: int | None = None) -> None:
        self.dsn: str = (dsn or _pgvector_dsn() or "").strip()
        self.dim: int = int(dim) if dim is not None else get_vector_dim()
        if self.dim < 8 or self.dim > 2048:
            self.dim = get_vector_dim()
        self._pool = None  # type: ignore
        self._enabled: bool = False
        self._last_error: str | None = None
        self._is_async: bool = False
        if self.dsn and self.dsn.startswith(_PG_PREFIXES):
            self._init_pool()

    def _init_pool(self) -> None:
        if not self.dsn:
            return
        # 延迟导入连接池；不可用时回退到直连 psycopg，避免硬依赖
        Pool = None  # type: ignore
        try:
            try:
                from psycopg_pool import AsyncConnectionPool as _AsyncPool  # type: ignore

                Pool = _AsyncPool  # type: ignore
            except Exception:
                from psycopg_pool import ConnectionPool as _SyncPool  # type: ignore

                Pool = _SyncPool  # type: ignore
        except Exception:
            Pool = None  # type: ignore
        if Pool is None:
            # 检查 psycopg 是否可用
            try:
                import importlib.util as _ilu

                if _ilu.find_spec("psycopg") is None:
                    self._last_error = "psycopg not installed"
                    return
                # 无连接池时改为每次直连，仍视为可用
                self._enabled = True
                return
            except Exception as e:
                self._last_error = str(e)
                return
        try:
            try:
                self._pool = Pool(conninfo=self.dsn, min_size=1, max_size=5, timeout=5, kwargs={"connect_timeout": 5})  # type: ignore
            except TypeError:
                try:
                    self._pool = Pool(conninfo=self.dsn, min_size=1, max_size=5)  # type: ignore
                except TypeError:
                    self._pool = Pool(self.dsn)  # type: ignore  # type: ignore
            # 识别是否为异步连接池
            try:
                import inspect as _ins

                self._is_async = "Async" in type(self._pool).__name__ or _ins.iscoroutinefunction(getattr(self._pool, "open", None))
            except Exception:
                self._is_async = False
            self._enabled = True
            # 仅同步池立即同步表结构，异步池延迟到使用时
            if not self._is_async:
                self._ensure_schema_sync()
        except Exception as e:
            self._last_error = str(e)
            self._pool = None
            self._enabled = False

    def _ensure_schema_sync(self) -> None:
        """同步侧车表结构，失败不影响主流程。"""
        if not self._enabled or self._is_async:
            return
        # DDL 防御式执行：扩展、表、索引均为 best-effort，失败静默
        try:
            if self._pool is not None and hasattr(self._pool, "connection"):
                with self._pool.connection() as conn:  # type: ignore
                    try:
                        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                    except Exception as _exc:
                        logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                        pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
                    # 优先创建 vector 类型列，失败则回退到 TEXT
                    created = False
                    try:
                        conn.execute(
                            f"CREATE TABLE IF NOT EXISTS memory_vectors (key TEXT PRIMARY KEY, content TEXT, embedding vector({self.dim}), namespace TEXT, created TIMESTAMPTZ DEFAULT now())"
                        )
                        created = True
                    except Exception:
                        try:
                            conn.execute(
                                "CREATE TABLE IF NOT EXISTS memory_vectors (key TEXT PRIMARY KEY, content TEXT, embedding TEXT, namespace TEXT, created TIMESTAMPTZ DEFAULT now())"
                            )
                            created = True
                        except Exception:
                            created = False
                    # 索引为 best-effort，ivfflat 仅在 vector 类型时有效
                    if created:
                        try:
                            # 仅 vector 类型成功时创建向量索引
                            conn.execute(
                                "CREATE INDEX IF NOT EXISTS idx_memory_vectors_embedding ON memory_vectors USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
                            )
                        except Exception as _exc:
                            logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                            pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
                        try:
                            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_vectors_ns ON memory_vectors (namespace)")
                        except Exception as _exc:
                            logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                            pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
                    try:
                        conn.commit()  # type: ignore
                    except Exception as _exc:
                        logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                        pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
            elif self._pool is not None and hasattr(self._pool, "getconn"):
                conn = self._pool.getconn()  # type: ignore
                try:
                    with conn.cursor() as cur:
                        try:
                            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                        except Exception as _exc:
                            logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                            pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
                        try:
                            cur.execute(
                                f"CREATE TABLE IF NOT EXISTS memory_vectors (key TEXT PRIMARY KEY, content TEXT, embedding vector({self.dim}), namespace TEXT, created TIMESTAMPTZ DEFAULT now())"
                            )
                        except Exception:
                            cur.execute(
                                "CREATE TABLE IF NOT EXISTS memory_vectors (key TEXT PRIMARY KEY, content TEXT, embedding TEXT, namespace TEXT, created TIMESTAMPTZ DEFAULT now())"
                            )
                        try:
                            cur.execute("CREATE INDEX IF NOT EXISTS idx_memory_vectors_ns ON memory_vectors (namespace)")
                        except Exception as _exc:
                            logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                            pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
                    conn.commit()
                finally:
                    try:
                        self._pool.putconn(conn)  # type: ignore
                    except Exception as _exc:
                        logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                        pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
            else:
                # 无连接池时走一次性直连路径
                try:
                    import psycopg  # type: ignore

                    with psycopg.connect(self.dsn, connect_timeout=5) as conn:  # type: ignore
                        with conn.cursor() as cur:
                            try:
                                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                            except Exception as _exc:
                                logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                                pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
                            try:
                                cur.execute(
                                    f"CREATE TABLE IF NOT EXISTS memory_vectors (key TEXT PRIMARY KEY, content TEXT, embedding vector({self.dim}), namespace TEXT, created TIMESTAMPTZ DEFAULT now())"
                                )
                            except Exception:
                                cur.execute(
                                    "CREATE TABLE IF NOT EXISTS memory_vectors (key TEXT PRIMARY KEY, content TEXT, embedding TEXT, namespace TEXT, created TIMESTAMPTZ DEFAULT now())"
                                )
                        conn.commit()
                except Exception as _exc:
                    logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                    pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
        except Exception as e:
            self._last_error = str(e)
            pass

    def ping(self) -> bool:
        """探测侧车连通性，任意路径成功即返回 True。"""
        if not self._enabled:
            return False
        try:
            if self._pool is not None and hasattr(self._pool, "connection") and not self._is_async:
                with self._pool.connection() as conn:  # type: ignore
                    try:
                        cur = conn.execute("SELECT 1")  # type: ignore
                        cur.fetchone()  # type: ignore
                    except Exception:
                        with conn.cursor() as cur:  # type: ignore
                            cur.execute("SELECT 1")
                            cur.fetchone()
                return True
            elif self._pool is not None and hasattr(self._pool, "getconn"):
                conn = self._pool.getconn()  # type: ignore
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        cur.fetchone()
                    return True
                finally:
                    try:
                        self._pool.putconn(conn)  # type: ignore
                    except Exception as _exc:
                        logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                        pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
            else:
                import psycopg  # type: ignore

                with psycopg.connect(self.dsn, connect_timeout=5) as conn:  # type: ignore
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        cur.fetchone()
                return True
        except Exception:
            return False

    def upsert(self, key: str, content: str, embedding: list[float], namespace: str | None = None) -> bool:
        """向侧车写入/更新一条向量记录，失败返回 False。"""
        if not self._enabled or not key:
            return False
        lit = _vector_to_literal(embedding)
        ns = namespace or ""
        # 异步池暂不支持同步写入，直接回退由调用方走本地
        if self._is_async:
            return False
        try:
            if self._pool is not None and hasattr(self._pool, "connection"):
                with self._pool.connection() as conn:  # type: ignore
                    # 优先按 vector 类型写入，失败则回退到 TEXT 列
                    try:
                        conn.execute(
                            "INSERT INTO memory_vectors (key, content, embedding, namespace) VALUES (%s, %s, %s::vector, %s) ON CONFLICT (key) DO UPDATE SET content=EXCLUDED.content, embedding=EXCLUDED.embedding, namespace=EXCLUDED.namespace",
                            (key, content, lit, ns),
                        )
                    except Exception:
                        # 回退到 TEXT 列的兼容写法
                        with conn.cursor() as cur:  # type: ignore
                            cur.execute(
                                "INSERT INTO memory_vectors (key, content, embedding, namespace) VALUES (%s, %s, %s, %s) ON CONFLICT (key) DO UPDATE SET content=EXCLUDED.content, embedding=EXCLUDED.embedding, namespace=EXCLUDED.namespace",
                                (key, content, lit, ns),
                            )
                    try:
                        conn.commit()  # type: ignore
                    except Exception as _exc:
                        logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                        pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
                return True
            elif self._pool is not None and hasattr(self._pool, "getconn"):
                conn = self._pool.getconn()  # type: ignore
                try:
                    with conn.cursor() as cur:
                        try:
                            cur.execute(
                                "INSERT INTO memory_vectors (key, content, embedding, namespace) VALUES (%s, %s, %s::vector, %s) ON CONFLICT (key) DO UPDATE SET content=EXCLUDED.content, embedding=EXCLUDED.embedding, namespace=EXCLUDED.namespace",
                                (key, content, lit, ns),
                            )
                        except Exception:
                            cur.execute(
                                "INSERT INTO memory_vectors (key, content, embedding, namespace) VALUES (%s, %s, %s, %s) ON CONFLICT (key) DO UPDATE SET content=EXCLUDED.content, embedding=EXCLUDED.embedding, namespace=EXCLUDED.namespace",
                                (key, content, lit, ns),
                            )
                    conn.commit()
                finally:
                    try:
                        self._pool.putconn(conn)  # type: ignore
                    except Exception as _exc:
                        logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                        pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
                return True
            else:
                import psycopg  # type: ignore

                with psycopg.connect(self.dsn, connect_timeout=5) as conn:  # type: ignore
                    with conn.cursor() as cur:
                        try:
                            cur.execute(
                                "INSERT INTO memory_vectors (key, content, embedding, namespace) VALUES (%s, %s, %s::vector, %s) ON CONFLICT (key) DO UPDATE SET content=EXCLUDED.content, embedding=EXCLUDED.embedding, namespace=EXCLUDED.namespace",
                                (key, content, lit, ns),
                            )
                        except Exception:
                            cur.execute(
                                "INSERT INTO memory_vectors (key, content, embedding, namespace) VALUES (%s, %s, %s, %s) ON CONFLICT (key) DO UPDATE SET content=EXCLUDED.content, embedding=EXCLUDED.embedding, namespace=EXCLUDED.namespace",
                                (key, content, lit, ns),
                            )
                    conn.commit()
                return True
        except Exception as e:
            self._last_error = str(e)
            return False

    def search(self, query_vec: list[float], top_k: int = 5, namespace: str | None = None) -> list[dict]:
        """在侧车中按余弦距离检索最相似的 top_k 条记录。"""
        if not self._enabled or not query_vec:
            return []
        lit = _vector_to_literal(query_vec)
        ns = namespace or ""
        top_k = max(1, min(int(top_k), 100))
        try:
            rows: list[tuple] = []
            if self._pool is not None and hasattr(self._pool, "connection") and not self._is_async:
                with self._pool.connection() as conn:  # type: ignore
                    try:
                        # 余弦距离：ORDER BY embedding <=> query 升序即最相似在前
                        if ns:
                            cur = conn.execute(  # type: ignore
                                "SELECT key, content, embedding FROM memory_vectors WHERE namespace=%s ORDER BY embedding <=> %s::vector LIMIT %s",
                                (ns, lit, top_k),
                            )
                            rows = cur.fetchall()  # type: ignore
                        else:
                            cur = conn.execute(  # type: ignore
                                "SELECT key, content, embedding FROM memory_vectors ORDER BY embedding <=> %s::vector LIMIT %s",
                                (lit, top_k),
                            )
                            rows = cur.fetchall()  # type: ignore
                    except Exception:
                        with conn.cursor() as cur:  # type: ignore
                            if ns:
                                cur.execute(
                                    "SELECT key, content, embedding FROM memory_vectors WHERE namespace=%s ORDER BY embedding <=> %s::vector LIMIT %s",
                                    (ns, lit, top_k),
                                )
                            else:
                                cur.execute(
                                    "SELECT key, content, embedding FROM memory_vectors ORDER BY embedding <=> %s::vector LIMIT %s",
                                    (lit, top_k),
                                )
                            rows = cur.fetchall()
            elif self._pool is not None and hasattr(self._pool, "getconn"):
                conn = self._pool.getconn()  # type: ignore
                try:
                    with conn.cursor() as cur:
                        if ns:
                            cur.execute(
                                "SELECT key, content, embedding FROM memory_vectors WHERE namespace=%s ORDER BY embedding <=> %s::vector LIMIT %s",
                                (ns, lit, top_k),
                            )
                        else:
                            cur.execute(
                                "SELECT key, content, embedding FROM memory_vectors ORDER BY embedding <=> %s::vector LIMIT %s",
                                (lit, top_k),
                            )
                        rows = cur.fetchall()
                finally:
                    try:
                        self._pool.putconn(conn)  # type: ignore
                    except Exception as _exc:
                        logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                        pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
            else:
                import psycopg  # type: ignore

                with psycopg.connect(self.dsn, connect_timeout=5) as conn:  # type: ignore
                    with conn.cursor() as cur:
                        if ns:
                            cur.execute(
                                "SELECT key, content, embedding FROM memory_vectors WHERE namespace=%s ORDER BY embedding <=> %s::vector LIMIT %s",
                                (ns, lit, top_k),
                            )
                        else:
                            cur.execute(
                                "SELECT key, content, embedding FROM memory_vectors ORDER BY embedding <=> %s::vector LIMIT %s",
                                (lit, top_k),
                            )
                        rows = cur.fetchall()
            out: list[dict] = []
            for r in rows:
                try:
                    k, c, _emb = r[0], r[1], r[2]
                    out.append({"key": k, "content": c})
                except Exception:
                    continue
            return out
        except Exception as e:
            self._last_error = str(e)
            return []


class MemoryStore:
    """记忆主存储：文件落地 + SQLite FTS5 索引 + 30 秒去重 + 向量混合检索。

    职责：统一管理记忆的持久化与召回；线程安全依赖 SQLite ``check_same_thread=False`` 与文件锁，索引一致性由同事务写入保障。
    关键状态：``_recent_hashes`` 为 30s 滑动窗口去重表，``_meta`` 为 Ebbinghaus 衰减的内存元数据，``_vector_enabled/_fts_enabled`` 标记能力可用性。
    """

    def __init__(self, base_path: Path | str, namespace: str | None = None):
        self.base = Path(base_path)
        self.base.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace
        self._lock = threading.RLock()
        self._recent_hashes: dict[str, float] = {}
        self._meta: dict[str, dict] = {}  # ns_key -> {quality_score, access_count, last_accessed}，内存态衰减元数据
        self._fts_enabled = False
        self._trigram_enabled = False
        self._bigram_enabled = False
        self._vector_enabled = False
        self.db_path = self.base / "memory.db"
        self._init_db()
        # 延迟导入层次路由，避免循环依赖
        try:
            from .hierarchy import MemoryHierarchy

            self.hierarchy = MemoryHierarchy(self.base)
        except Exception:
            self.hierarchy = None  # type: ignore
        # pgvector 侧车：已配置则作为一等公民，否则回退到本地混合检索
        self._pgvector: PgVectorSidecar | None = None
        self._pgvector_enabled: bool = False
        try:
            if is_pgvector_configured():
                # 维度以 Settings/env 为准，统一由 get_vector_dim 提供
                self._pgvector = PgVectorSidecar(dim=get_vector_dim())
                # 即使连接失败也保留对象以便诊断
                self._pgvector_enabled = bool(getattr(self._pgvector, "_enabled", False))
                # 侧车不可用时本地向量仍保持可用
                if self._pgvector_enabled:
                    self._vector_enabled = True
            else:
                self._pgvector = None
                self._pgvector_enabled = False
        except Exception:
            self._pgvector = None
            self._pgvector_enabled = False
        # 检索结果缓存（TTL 30s，Wave6 P2）—— key: (query, top_k, namespace, vector_backend)
        self._retrieval_cache: dict[tuple, tuple[float, list[dict]]] = {}
        self._retrieval_cache_ttl: float = 30.0
        self._vector_cache: dict[tuple, tuple[float, list[dict]]] = {}
        self._vector_cache_ttl: float = 30.0

    def _cache_get(self, cache: dict, key: tuple, ttl: float) -> list[dict] | None:
        try:
            with self._lock:
                ts, val = cache.get(key, (None, None))  # type: ignore
                if ts is None:
                    return None
                if (time.time() - float(ts)) > ttl:
                    try:
                        cache.pop(key, None)
                    except Exception:
                        pass
                    return None
                # 返回深拷贝以防调用方篡改缓存
                if isinstance(val, list):
                    return [dict(x) for x in val]  # type: ignore
                return val  # type: ignore
        except Exception:
            return None

    def _cache_set(self, cache: dict, key: tuple, val: list[dict]) -> None:
        try:
            with self._lock:
                cache[key] = (time.time(), [dict(x) for x in val])
                # 简单容量控制：超过 256 条时淘汰最早
                if len(cache) > 256:
                    oldest = min(cache.items(), key=lambda kv: kv[1][0])[0]
                    cache.pop(oldest, None)
        except Exception:
            pass

    def clear_retrieval_cache(self) -> None:
        """清空检索缓存（测试/写入后失效）。"""
        try:
            with self._lock:
                self._retrieval_cache.clear()
                self._vector_cache.clear()
        except Exception:
            pass

    def close(self) -> None:
        """Idempotent close - release sqlite handle, safe for Windows tmp_path cleanup."""
        try:
            with self._lock:
                # clear caches to free memory (bounded growth)
                try:
                    self._retrieval_cache.clear()
                    self._vector_cache.clear()
                except Exception:
                    pass
                # close sqlite connection if open
                conn = getattr(self, "_conn", None)
                if conn is not None:
                    try:
                        real = getattr(conn, "_real", conn)
                        try:
                            real.commit()
                        except Exception:
                            pass
                        try:
                            real.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        except Exception:
                            pass
                        real.close()
                    except Exception as e:
                        logger.debug("MemoryStore close failed: %s", e)
                    finally:
                        # mark as closed to make idempotent
                        self._conn = None  # type: ignore
        except Exception as e:
            logger.debug("MemoryStore close outer failed: %s", e)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.close()
        except Exception:
            pass
        return False

    @property
    def vector_backend(self) -> str:
        """返回当前向量后端：``pgvector`` 或 ``local``。"""
        if self._pgvector is not None and getattr(self._pgvector, "_enabled", False):
            # 仅做标识，不在属性访问中阻塞式 ping
            return "pgvector"
        return "local"

    def get_vector_backend(self) -> str:
        """返回当前向量后端标识。"""
        return self.vector_backend

    def is_pgvector_enabled(self) -> bool:
        """侧车是否已启用且就绪。"""
        return bool(self._pgvector_enabled and self._pgvector is not None and getattr(self._pgvector, "_enabled", False))

    def _ns_key(self, key: str) -> str:
        """按 namespace 拼接存储键。"""
        if self.namespace:
            return f"{self.namespace}:{key}"
        return key

    def _ns_prefix(self) -> str | None:
        if self.namespace:
            return f"{self.namespace}:"
        return None

    def _safe_filename(self, ns_key: str) -> str:
        """生成文件安全名，规避 Windows 禁用字符与路径穿越。"""
        # 使用 ``__NS__`` 作为命名空间分隔，避免与内容中 ``__`` 歧义
        safe = ns_key.replace(":", "__NS__").replace("/", "__NS__").replace("\\", "__NS__")
        # 阻断 ``..`` 穿越
        safe = safe.replace("..", "__NS__")
        return f"{safe}.md"

    def _safe_prefix(self) -> str | None:
        """返回用于文件过滤的安全前缀。"""
        if self.namespace:
            # 与 _safe_filename 保持同构替换
            return self.namespace.replace(":", "__NS__").replace("/", "__NS__").replace("\\", "__NS__") + "__NS__"
        return None

    def _safe_prefix_old(self) -> str | None:
        """旧 ``__`` 前缀，用于向后兼容过滤。"""
        if self.namespace:
            return self.namespace.replace(":", "__").replace("/", "__").replace("\\", "__") + "__"
        return None

    def _matches_safe_prefix(self, filename: str) -> bool:
        """兼容新 ``__NS__`` 与旧 ``__`` 前缀的过滤判断；无 namespace 时始终 True。"""
        if self.namespace is None:
            return True
        new_p = self._safe_prefix()
        old_p = self._safe_prefix_old()
        return (new_p is not None and filename.startswith(new_p)) or (old_p is not None and filename.startswith(old_p))

    def _parse_safe_stem(self, stem: str) -> str:
        """将安全文件名 stem 还原为原始 ns_key，兼容旧 ``__`` 分隔。"""
        if "__NS__" in stem:
            return stem.replace("__NS__", ":")
        # 向后兼容旧文件：``__`` 分隔
        return stem.replace("__", ":")

    def is_safe_filename(self, name: str) -> bool:
        """校验文件名是否符合安全命名规范（新 ``__NS__`` 或旧 ``__`` 兼容）。"""
        if not name.endswith(".md"):
            return False
        stem = name[:-3]
        if not stem or "/" in stem or "\\" in stem or ":" in stem:
            return False
        if ".." in stem:
            return False
        return True

    def _init_db(self) -> None:
        """初始化 SQLite：建表、补齐向量列、创建 FTS5 索引。"""
        _raw = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=10.0)
        try:
            _raw.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        try:
            _raw.execute("PRAGMA busy_timeout=5000")
        except Exception:
            pass
        self._conn = _SQLiteConnWrapper(_raw)
        # 主表为检索索引，真实来源仍是文件
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, content TEXT, created TEXT)"
        )
        # 兼容老库：按需补齐向量列
        try:
            cur = self._conn.cursor()
            cur.execute("PRAGMA table_info(notes)")
            cols = [row[1] for row in cur.fetchall()]
            if "vector" not in cols:
                try:
                    self._conn.execute("ALTER TABLE notes ADD COLUMN vector TEXT")
                    self._conn.commit()
                except sqlite3.OperationalError:
                    pass
            # 二次探查列存在性以决定向量能力
            cur.execute("PRAGMA table_info(notes)")
            cols2 = [row[1] for row in cur.fetchall()]
            self._vector_enabled = "vector" in cols2 or "embedding" in cols2
            # 即使缺列也启用向量逻辑，改为即时计算，保证测试与旧库可用
            if "vector" in cols2:
                self._vector_enabled = True
            else:
                # 无列时走即时 embedding 回退
                self._vector_enabled = True
        except Exception:
            self._vector_enabled = True
        # 创建 FTS5 虚表
        try:
            # 优先 trigram：对中英文无空格场景召回更友好
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(content, tokenize='trigram')"
            )
            self._fts_enabled = True
            self._trigram_enabled = True
        except Exception:
            try:
                self._conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(content)"
                )
                self._fts_enabled = True
            except Exception:
                self._fts_enabled = False

        # trigram 不支持短 token；独立的内容 bigram 表覆盖短查询及 trigram 建表失败。
        try:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts_bigram USING fts5(bigrams)"
            )
            cur = self._conn.cursor()
            cur.execute("SELECT id, content FROM notes")
            for rowid, content in cur.fetchall():
                cur.execute(
                    "INSERT OR REPLACE INTO notes_fts_bigram (rowid, bigrams) VALUES (?, ?)",
                    (rowid, _content_bigrams(content)),
                )
            self._bigram_enabled = True
        except Exception:
            self._bigram_enabled = False
        self._conn.commit()

    def _embed_text(self, text: str):
        """对文本做 embedding，延迟导入以避免循环依赖。"""
        try:
            from hero_quant.agent.embed import embed  # type: ignore

            return embed(text)
        except Exception:
            # 回退：基于哈希的 32 维确定性向量
            import hashlib as _hl

            h = _hl.sha256(text.encode("utf-8")).digest()
            vals = []
            counter = 0
            while len(vals) < 32:
                chunk = _hl.sha256(h + counter.to_bytes(2, "little")).digest() if counter else h
                for b in chunk:
                    if len(vals) >= 32:
                        break
                    vals.append(b / 255.0)
                counter += 1
            return vals[:32]

    def _cosine_sim(self, a, b) -> float:
        """计算余弦相似度，优先委托 embed 模块，失败则本地计算。"""
        try:
            from hero_quant.agent.embed import cosine_sim  # type: ignore

            return cosine_sim(a, b)
        except Exception:
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(y * y for y in b))
            if na == 0 or nb == 0:
                return 0.0
            return dot / (na * nb)

    def _load_vector_for_key(self, key: str):
        """按 key 载入已存向量；维度漂移时视为过期返回 None 触发重算。"""
        try:
            cur = self._conn.cursor()
            # 先确认向量列存在，避免旧库报错
            cur.execute("PRAGMA table_info(notes)")
            cols = [row[1] for row in cur.fetchall()]
            if "vector" not in cols:
                return None
            cur.execute("SELECT vector FROM notes WHERE key = ? ORDER BY id DESC LIMIT 1", (key,))
            row = cur.fetchone()
            if row and row[0]:
                raw = row[0]
                if isinstance(raw, str):
                    try:
                        parsed = json.loads(raw)
                    except Exception:
                        return None
                else:
                    parsed = raw
                # 维度漂移校验：以当前 get_vector_dim 为准
                try:
                    from hero_quant.agent.embed import get_vector_dim as _gvd

                    expected = int(_gvd())
                    if isinstance(parsed, list) and len(parsed) != expected:
                        return None
                except Exception as _exc:
                    logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                    pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
                return parsed
        except Exception:
            return None
        return None

    def _ensure_vector_dim(self, vec, content: str):
        """向量为空或维度不匹配时重算，保证与当前 dim 一致。"""
        try:
            from hero_quant.agent.embed import get_vector_dim as _gvd

            expected = int(_gvd())
            if vec is None or not isinstance(vec, list) or len(vec) != expected:
                return self._embed_text(content)
            return vec
        except Exception:
            return vec if vec is not None else self._embed_text(content)

    # pgvector 侧车辅助
    def _pgvector_upsert(self, ns_key: str, content: str, vec) -> None:
        """尽力写入侧车向量，失败静默回退，不抛异常。"""
        if not self._pgvector_enabled or self._pgvector is None or vec is None:
            return
        try:
            # 侧车按 namespace 隔离存储，检索时可按 namespace 过滤
            ns = self.namespace or ""
            ok = self._pgvector.upsert(ns_key, content, vec, namespace=ns)
            if not ok:
                # 失败不永久禁用，保留下次重试机会
                pass
        except Exception as _exc:
            logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
            pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local

    def _pgvector_search(self, query: str, top_k: int = 5) -> list[dict]:
        """尝试侧车检索，失败返回空列表以触发本地回退。"""
        if not self._pgvector_enabled or self._pgvector is None:
            return []
        if not query:
            return []
        try:
            qvec = self._embed_text(query)
            if qvec is None:
                return []
            ns = self.namespace or "" if self.namespace else None
            # 调用侧车检索
            pg_hits = self._pgvector.search(qvec, top_k=top_k, namespace=ns)
            # 侧车已按 namespace 过滤时仍做二次前缀校验，防御不一致
            # 侧车前后缀双重保障：即使后端未过滤也能正确隔离
            if self._ns_prefix() is not None and pg_hits:
                prefix = self._ns_prefix()
                pg_hits = [h for h in pg_hits if h.get("key", "").startswith(prefix or "")]
            # 侧车未直接返回距离时本地补算余弦分数用于混合重排
            out: list[dict] = []
            for h in pg_hits:
                # 本地重算相似度以统一混合打分
                try:
                    cvec = self._load_vector_for_key(h["key"])
                    if cvec is None:
                        cvec = self._embed_text(h["content"])
                    score = self._cosine_sim(qvec, cvec) if cvec is not None else 0.0
                except Exception:
                    score = 0.0
                out.append({"key": h["key"], "content": h["content"], "score": score})
            return out
        except Exception:
            return []

    def write(self, key: str, content: str, memory_type: str | None = None) -> None:
        """写入一条记忆：经去重、落盘、建索引并同步向量侧车。"""
        # P2: missing validation - fail-visible for empty key/content
        if not isinstance(key, str) or not key.strip():
            logger.warning("MemoryStore.write rejected empty key %r", key)
            raise ValueError("key must be non-empty str")
        if not isinstance(content, str) or not content.strip():
            logger.warning("MemoryStore.write rejected empty content for key %r", key)
            raise ValueError("content must be non-empty str")
        now = time.time()
        ns_key = self._ns_key(key)
        # 30 秒滑动窗口去重：跨 key、大小写/空白归一
        with self._lock:
            # 定时清理过期哈希，避免内存膨胀
            self._recent_hashes = {h: ts for h, ts in self._recent_hashes.items() if now - ts < 30}
            # P2: unbounded growth - cap _recent_hashes to 2048 entries (LRU by timestamp)
            if len(self._recent_hashes) > 2048:
                # evict oldest by timestamp
                sorted_items = sorted(self._recent_hashes.items(), key=lambda kv: kv[1])
                # keep newest 2048
                self._recent_hashes = dict(sorted_items[-2048:])
                logger.debug("recent_hashes capped to 2048, evicted %d", len(sorted_items) - 2048)
            # 内容归一哈希为去重核心
            content_hash = hashlib.sha256(content.lower().strip().encode()).hexdigest()[:12]
            # 同时校验 name:content 组合哈希，覆盖同内容不同键的重复
            full_hash = _content_hash(ns_key, content)
            if content_hash in self._recent_hashes or full_hash in self._recent_hashes:
                return
            self._recent_hashes[content_hash] = now
            self._recent_hashes[full_hash] = now
            # re-check cap after insert
            if len(self._recent_hashes) > 2048:
                sorted_items = sorted(self._recent_hashes.items(), key=lambda kv: kv[1])
                self._recent_hashes = dict(sorted_items[-2048:])

        # 原子落盘：tmp 写入 -> fsync -> os.replace，权限 0600，兼容 flock
        # 层次路由：memory_type 命中 CATEGORIES 时分目录，其余回落到 base
        safe_name = self._safe_filename(ns_key)
        if memory_type:
            try:
                from .hierarchy import CATEGORIES

                if memory_type in CATEGORIES and self.hierarchy is not None:
                    file_path = self.hierarchy.route_entry(memory_type, safe_name)
                else:
                    # 未知类型回落到 base 目录并告警
                    if memory_type not in CATEGORIES and memory_type:
                        import logging

                        logging.getLogger(__name__).warning(
                            "Unknown memory_type '%s', routing to base dir", memory_type
                        )
                    file_path = self.base / safe_name
            except Exception:
                file_path = self.base / safe_name
        else:
            file_path = self.base / safe_name
        tmp_path = self.base / f".{safe_name}.tmp.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:6]}"  # O_EXCL unique tmp via mkstemp semantics
        # 兼容层次路由的子目录结构，确保父目录存在
        file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _oflag = os.O_WRONLY | os.O_CREAT | os.O_EXCL | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0)
            _fd = os.open(tmp_path, _oflag, 0o600)
            with os.fdopen(_fd, "w", encoding="utf-8") as f:
                try:
                    import fcntl  # type: ignore

                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                except ImportError:
                    # Windows 回退：尝试 msvcrt 文件锁
                    try:
                        import msvcrt  # type: ignore

                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    except Exception as _exc:
                        logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                        pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
                except Exception as _exc:
                    logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                    pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
                f.write(content)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception as _exc:
                    logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                    pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
            try:
                os.chmod(tmp_path, 0o600)
            except Exception as _exc:
                logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
            os.replace(tmp_path, file_path)
            try:
                os.chmod(file_path, 0o600)
            except Exception as _exc:
                logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception as _exc:
                    logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                    pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local

        # 以 namespace 键写入 SQLite 索引（含向量列）
        created = datetime.now(timezone.utc).isoformat()
        # 计算向量（可插拔 embedding）
        vector_json = None
        try:
            vec = self._embed_text(content)
            if vec is not None:
                vector_json = json.dumps(vec)
        except Exception:
            vector_json = None
        try:
            cur = self._conn.cursor()
            # 探查向量列是否存在以选择写入路径
            cur.execute("PRAGMA table_info(notes)")
            cols = [row[1] for row in cur.fetchall()]
            has_vector = "vector" in cols
            if has_vector:
                cur.execute(
                    "INSERT INTO notes (key, content, created, vector) VALUES (?, ?, ?, ?)",
                    (ns_key, content, created, vector_json),
                )
            else:
                cur.execute(
                    "INSERT INTO notes (key, content, created) VALUES (?, ?, ?)",
                    (ns_key, content, created),
                )
                # 向量列后续出现时可通过更新回填，此处占位
                if vector_json is not None:
                    try:
                        # 预留更新路径，当前无操作
                        pass
                    except Exception as _exc:
                        logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                        pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
            rowid = cur.lastrowid
            if self._fts_enabled:
                try:
                    cur.execute(
                        "INSERT INTO notes_fts (rowid, content) VALUES (?, ?)",
                        (rowid, content),
                    )
                except Exception:
                    # 回退：不依赖 rowid 的插入
                    try:
                        cur.execute(
                            "INSERT INTO notes_fts (content) VALUES (?)", (content,)
                        )
                    except Exception as _exc:
                        logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                        pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
            if self._bigram_enabled:
                try:
                    cur.execute(
                        "INSERT INTO notes_fts_bigram (rowid, bigrams) VALUES (?, ?)",
                        (rowid, _content_bigrams(content)),
                    )
                except Exception as _exc:
                    logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                    pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
            self._conn.commit()
            # 侧车同步：尽力而为，失败不影响本地写入事务
            try:
                if vector_json is not None:
                    try:
                        vec_obj = json.loads(vector_json) if isinstance(vector_json, str) else vector_json
                    except Exception:
                        vec_obj = None
                    if vec_obj is not None:
                        self._pgvector_upsert(ns_key, content, vec_obj)
                else:
                    # 本地向量缺失时即时计算再同步，保证维度一致
                    try:
                        vec2 = self._embed_text(content)
                        if vec2 is not None:
                            self._pgvector_upsert(ns_key, content, vec2)
                    except Exception as _exc:
                        logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                        pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
            except Exception as _exc:
                logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
        except Exception as _e:
            try:
                self._conn.rollback()
            except Exception as _exc:
                logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
            # atomic double-write failure: clean orphan file or reconcile
            try:
                if file_path.exists():
                    file_path.unlink()
                logger.warning("write DB failed, cleaned orphan file; reconcile may be needed", exc_info=_e)
            except Exception as _exc:
                logger.warning("reconcile: failed to clean orphan file", exc_info=_exc)
                pass
        # 初始化 Ebbinghaus 元数据：内存态，无需 DDL
        with self._lock:
            if ns_key not in self._meta:
                self._meta[ns_key] = {"quality_score": 0.5, "access_count": 0, "last_accessed": now}
            else:
                # 已有元数据保持不变，避免覆盖外部更新的访问计数
                pass
            # P2: unbounded growth - cap _meta to 4096 entries, evict oldest by last_accessed
            if len(self._meta) > 4096:
                sorted_meta = sorted(self._meta.items(), key=lambda kv: kv[1].get("last_accessed", 0))
                # keep newest 4096
                keep = dict(sorted_meta[-4096:])
                self._meta.clear()
                self._meta.update(keep)
                logger.debug("meta capped to 4096, evicted %d", len(sorted_meta) - 4096)
        # 写入后失效检索缓存，保证新笔记立即可召回
        try:
            self.clear_retrieval_cache()
        except Exception:
            pass

    def index_external(self, key: str, content: str) -> None:
        """索引已由外部文件持久化的内容，不重复写入记忆文件。"""
        now = time.time()
        ns_key = self._ns_key(key)
        vector = None
        vector_json = None
        try:
            vector = self._embed_text(content)
            if vector is not None:
                try:
                    vector_json = json.dumps(vector)
                except Exception:
                    vector_json = None
        except Exception as _exc:
            logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
            pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local

        try:
            cur = self._conn.cursor()
            cur.execute("SELECT id FROM notes WHERE key = ?", (ns_key,))
            old_rowids = [row[0] for row in cur.fetchall()]
            for rowid in old_rowids:
                for table in ("notes_fts", "notes_fts_bigram"):
                    try:
                        cur.execute(f"DELETE FROM {table} WHERE rowid = ?", (rowid,))
                    except Exception as _exc:
                        logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                        pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
            cur.execute("DELETE FROM notes WHERE key = ?", (ns_key,))

            cur.execute("PRAGMA table_info(notes)")
            columns = [row[1] for row in cur.fetchall()]
            if "vector" in columns:
                cur.execute(
                    "INSERT INTO notes (key, content, created, vector) VALUES (?, ?, ?, ?)",
                    (ns_key, content, datetime.now(timezone.utc).isoformat(), vector_json),
                )
            else:
                cur.execute(
                    "INSERT INTO notes (key, content, created) VALUES (?, ?, ?)",
                    (ns_key, content, datetime.now(timezone.utc).isoformat()),
                )
            rowid = cur.lastrowid
            if self._fts_enabled:
                try:
                    cur.execute(
                        "INSERT INTO notes_fts (rowid, content) VALUES (?, ?)",
                        (rowid, content),
                    )
                except Exception as _exc:
                    logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                    pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
            if self._bigram_enabled:
                try:
                    cur.execute(
                        "INSERT INTO notes_fts_bigram (rowid, bigrams) VALUES (?, ?)",
                        (rowid, _content_bigrams(content)),
                    )
                except Exception as _exc:
                    logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                    pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
            self._conn.commit()
        except Exception:
            try:
                self._conn.rollback()
            except Exception as _exc:
                logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
            return

        if vector is not None:
            self._pgvector_upsert(ns_key, content, vector)
        if ns_key not in self._meta:
            self._meta[ns_key] = {"quality_score": 0.5, "access_count": 0, "last_accessed": now}
        try:
            self.clear_retrieval_cache()
        except Exception:
            pass

    def _importance_for(self, item: dict, now: float) -> float:
        """按 Ebbinghaus 14 天衰减计算单条记忆的重要性。"""
        ns_key = item.get("key", "")
        meta = self._meta.get(ns_key)
        if meta is None:
            # 文件扫描场景下 key 为安全文件名，需反向映射到原始 ns_key
            for k, v in self._meta.items():
                if self._safe_filename(k).removesuffix(".md") == ns_key:
                    meta = v
                    break
            if meta is None:
                # 兼容带 namespace 前缀的后缀匹配
                for k, v in self._meta.items():
                    if ns_key.endswith(k.split(":")[-1]) or k.endswith(ns_key.split(":")[-1]):
                        meta = v
                        break
        if meta is not None:
            qs = float(meta.get("quality_score", 0.5))
            ac = int(meta.get("access_count", 0))
            last = float(meta.get("last_accessed", now))
        else:
            qs, ac, last = 0.5, 0, now
        days = max(0.0, (now - last) / 86400.0)
        try:
            return compute_importance(qs, ac, days)
        except Exception:
            return qs

    def _rank_with_decay(self, items: list[dict]) -> list[dict]:
        """按衰减后的重要性对候选集重排。"""
        if not items or len(items) <= 1:
            return items
        now = time.time()
        scored: list[tuple[float, float, dict]] = []
        for it in items:
            imp = self._importance_for(it, now)
            weighted = 1.0 * (0.5 + 0.5 * imp)  # 基准分 1，按重要性线性加权
            scored.append((weighted, imp, it))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [it for _, _, it in scored]

    def vector_search(self, query: str, top_k: int = 5) -> list[dict]:
        """纯向量余弦 topK 检索，混合检索的子组件（30s TTL 缓存）。

        优先走 pgvector 侧车，失败回退到本地向量列或即时计算；全程按 namespace 隔离。
        """
        if not query:
            return []
        # TTL 缓存（Wave6）：命中则直接返回，避免重复 embedding/cosine
        try:
            _ck = (query, int(top_k), self.namespace or "", self.vector_backend)
            _cached = self._cache_get(self._vector_cache, _ck, self._vector_cache_ttl)
            if _cached is not None:
                return _cached
        except Exception:
            _ck = None  # type: ignore
            _cached = None  # type: ignore
        # 侧车一等公民：先尝试 pgvector，再与本地结果融合
        try:
            pg_hits = self._pgvector_search(query, top_k=top_k)
            if pg_hits:
                # 命中充足则直接返回，否则保留以便后续与本地合并
                if len(pg_hits) >= max(1, int(top_k)):
                    res_pg = pg_hits[: max(1, int(top_k))]
                    try:
                        if _ck is not None:
                            self._cache_set(self._vector_cache, _ck, res_pg)
                    except Exception:
                        pass
                    return res_pg
                # 命中不足，稍后与本地候选合并
            else:
                pg_hits = []
        except Exception:
            pg_hits = []
        prefix = self._ns_prefix()
        # 对查询做 embedding
        try:
            qvec = self._embed_text(query)
        except Exception:
            return pg_hits  # embedding 失败则仅返回侧车结果
        # 载入本地候选
        candidates: list[dict] = []
        try:
            cur = self._conn.cursor()
            # 探查向量列可用性
            cur.execute("PRAGMA table_info(notes)")
            cols = [row[1] for row in cur.fetchall()]
            has_vector = "vector" in cols
            if has_vector:
                cur.execute("SELECT key, content, vector FROM notes")
                rows = cur.fetchall()
                for k, c, v in rows:
                    if prefix is not None and not k.startswith(prefix):
                        continue
                    # 解析已存向量
                    note_vec = None
                    if v:
                        try:
                            note_vec = json.loads(v) if isinstance(v, str) else v
                        except Exception:
                            note_vec = None
                    # 维度漂移时重算，保证与当前 dim 一致
                    note_vec = self._ensure_vector_dim(note_vec, c)
                    if note_vec is None:
                        continue
                    sim = self._cosine_sim(qvec, note_vec)
                    candidates.append({"key": k, "content": c, "vector": note_vec, "_score": sim})
            else:
                # 无向量列时全量即时计算，兼容旧库
                cur.execute("SELECT key, content FROM notes")
                rows = cur.fetchall()
                for k, c in rows:
                    if prefix is not None and not k.startswith(prefix):
                        continue
                    note_vec = self._embed_text(c)
                    sim = self._cosine_sim(qvec, note_vec)
                    candidates.append({"key": k, "content": c, "_score": sim})
        except Exception:
            # 数据库异常时回退到文件扫描
            candidates = []
        # 数据库无候选时回退扫描文件，兼顾层次目录
        if not candidates:
            try:
                from .hierarchy import MemoryHierarchy

                mh = MemoryHierarchy(self.base)
                files = mh.scan_all()
            except Exception:
                files = list(self.base.rglob("*.md"))
                files = [p for p in files if "archive" not in p.parts]
            for md_file in files:
                try:
                    if not self._matches_safe_prefix(md_file.name):
                        continue
                    txt = md_file.read_text(encoding="utf-8")
                    # 从文件名反推原始 key，兼容 __NS__ 与旧 __
                    derived_key = self._parse_safe_stem(md_file.stem)
                    if prefix is not None and not derived_key.startswith(prefix.rstrip(":")):
                        # 二次校验安全前缀，避免命名空间串扰
                        if not self._matches_safe_prefix(md_file.name):
                            continue
                    note_vec = self._embed_text(txt)
                    sim = self._cosine_sim(qvec, note_vec)
                    candidates.append({"key": derived_key, "content": txt, "_score": sim})
                except Exception:
                    continue
        # 按余弦相似度降序
        candidates.sort(key=lambda x: x.get("_score", 0.0), reverse=True)
        # 融合侧车与本地结果：按 key 去重，保留更高分数
        if pg_hits:
            # 以本地候选建表便于分数对比
            # 以侧车结果为基准，保留其排序
            merged: dict[str, dict] = {}
            for h in pg_hits:
                merged[h["key"]] = {"key": h["key"], "content": h["content"], "_score": float(h.get("score", 0.0))}
            # 补充侧车未覆盖的本地候选
            for c in candidates:
                if c["key"] not in merged:
                    merged[c["key"]] = c
                else:
                    # 同 key 取更高分数
                    if float(c.get("_score", 0.0)) > float(merged[c["key"]].get("_score", 0.0)):
                        merged[c["key"]]["_score"] = float(c.get("_score", 0.0))
            # 按分数重排后截取 top_k
            all_items = list(merged.values())
            all_items.sort(key=lambda x: float(x.get("_score", 0.0)), reverse=True)
            out: list[dict] = []
            for it in all_items[: max(0, top_k)]:
                out.append({"key": it["key"], "content": it["content"], "score": float(it.get("_score", 0.0))})
            try:
                if _ck is not None:
                    self._cache_set(self._vector_cache, _ck, out)
            except Exception:
                pass
            return out
        # 剔除内部 _score，仅暴露 score
        out: list[dict] = []
        for it in candidates[: max(0, top_k)]:
            # 保留 key/content/score 三元组
            out.append({"key": it["key"], "content": it["content"], "score": it.get("_score", 0.0)})
        try:
            if _ck is not None:
                self._cache_set(self._vector_cache, _ck, out)
        except Exception:
            pass
        return out

    def _search_bigram_raw(self, query: str) -> list[dict]:
        """使用内容侧预切 bigram 表检索，失败时返回空列表。"""
        if not self._bigram_enabled or len(query) < 2:
            return []
        tokens = _content_bigrams(query).split()
        if not tokens:
            return []
        # Quote each token so punctuation in user input cannot become FTS syntax.
        match_query = " ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
        try:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT notes.key, notes.content "
                "FROM notes_fts_bigram JOIN notes ON notes_fts_bigram.rowid = notes.id "
                "WHERE notes_fts_bigram MATCH ?",
                (match_query,),
            )
            rows = cur.fetchall()
            prefix = self._ns_prefix()
            result = [{"key": key, "content": content} for key, content in rows]
            if prefix is not None:
                result = [item for item in result if item["key"].startswith(prefix)]
            seen: set[str] = set()
            return [
                item
                for item in result
                if not (item["content"] in seen or seen.add(item["content"]))
            ]
        except Exception:
            return []

    def _search_bm25_raw(self, query: str) -> list[dict]:
        """不含向量的 BM25/FTS/LIKE/文件回退检索，返回去重后的候选。"""
        if not query:
            return []
        prefix = self._ns_prefix()
        # trigram 对少于三个字符的 query 不可用；trigram 建表失败时所有 query 走此回退。
        if len(query) < 3 or not self._trigram_enabled:
            bigram_result = self._search_bigram_raw(query)
            if bigram_result:
                return bigram_result
        # 优先走 FTS5 MATCH — FTS MATCH 转义加引号防止语法注入
        if self._fts_enabled:
            try:
                cur = self._conn.cursor()
                match_query = f'"{query.replace(chr(34), chr(34) * 2)}"'
                cur.execute(
                    "SELECT notes.key, notes.content FROM notes_fts JOIN notes ON notes_fts.rowid = notes.id WHERE notes_fts MATCH ?",
                    (match_query,),
                )
                rows = cur.fetchall()
                if rows:
                    result = [{"key": k, "content": c} for k, c in rows]
                    if prefix is not None:
                        result = [r for r in result if r["key"].startswith(prefix)]
                        if not result:
                            raise sqlite3.OperationalError("no rows for namespace, fallback to LIKE")
                    seen: dict[str, dict] = {}
                    deduped: list[dict] = []
                    for item in result:
                        if item["content"] not in seen:
                            seen[item["content"]] = item
                            deduped.append(item)
                    if deduped:
                        return deduped
            except sqlite3.OperationalError:
                pass
            except Exception as _exc:
                logger.debug("silent handled: offline-safe: memory sidecar/pgvector optional, fallback to local", exc_info=_exc)  # intentional: offline-safe: memory sidecar/pgvector optional, fallback to local
                pass  # intentional offline-safe: memory sidecar/pgvector optional, fallback to local
        # FTS 异常或无命中时仍尝试 bigram，再降级到内容 LIKE。
        bigram_result = self._search_bigram_raw(query)
        if bigram_result:
            return bigram_result
        # 回退到 LIKE 模糊匹配
        try:
            cur = self._conn.cursor()
            pattern = f"%{query}%"
            if prefix is not None:
                cur.execute(
                    "SELECT key, content FROM notes WHERE key LIKE ? AND content LIKE ?",
                    (f"{prefix}%", pattern),
                )
            else:
                cur.execute("SELECT key, content FROM notes WHERE content LIKE ?", (pattern,))
            rows = cur.fetchall()
            result = [{"key": k, "content": c} for k, c in rows]
            if prefix is not None:
                result = [r for r in result if r["key"].startswith(prefix)]
            if not result:
                try:
                    from .hierarchy import MemoryHierarchy

                    mh = MemoryHierarchy(self.base)
                    candidates = mh.scan_all()
                except Exception:
                    candidates = list(self.base.rglob("*.md"))
                    candidates = [p for p in candidates if "archive" not in p.parts]
                for md_file in candidates:
                    try:
                        if not self._matches_safe_prefix(md_file.name):
                            continue
                        if prefix is not None and not self._parse_safe_stem(md_file.stem).startswith(prefix.rstrip(":")):
                            if not self._matches_safe_prefix(md_file.name):
                                continue
                        txt = md_file.read_text(encoding="utf-8")
                        if query in txt:
                            result.append({"key": self._parse_safe_stem(md_file.stem), "content": txt})
                    except Exception:
                        continue
            seen2: dict[str, dict] = {}
            deduped2: list[dict] = []
            for item in result:
                if item["content"] not in seen2:
                    seen2[item["content"]] = item
                    deduped2.append(item)
            return deduped2
        except Exception:
            return []

    def recall(self, query: str, top_k: int = 5) -> list[dict]:
        """Alias for search — Wave4 loop wiring uses recall naming."""
        try:
            return self.search(query)[: max(0, int(top_k))]
        except Exception:
            return []

    def search(self, query: str) -> list[dict]:
        """混合检索：BM25 召回 + 向量余弦 via rank_fusion (0.5/0.5) + 可选 Cohere 重排（30s TTL）。"""
        if not query:
            return []
        try:
            _sck = (query, self.namespace or "", self.vector_backend)
            _scached = self._cache_get(self._retrieval_cache, _sck, self._retrieval_cache_ttl)
            if _scached is not None:
                return _scached
        except Exception:
            _sck = None  # type: ignore
            _scached = None  # type: ignore
        # 文本召回候选
        bm25_candidates = self._search_bm25_raw(query)
        # 向量召回候选
        vector_candidates: list[dict] = []
        if self._vector_enabled:
            try:
                vector_candidates = self.vector_search(query, top_k=10)
            except Exception:
                vector_candidates = []
        # 两路皆空则直接返回
        if not bm25_candidates and not vector_candidates:
            return []
        # 按 key 合并两路去重，向量侧优先
        merged: dict[str, dict] = {}
        for item in vector_candidates:
            k = item["key"]
            if k not in merged:
                merged[k] = {"key": k, "content": item["content"]}
        for item in bm25_candidates:
            k = item["key"]
            if k not in merged:
                merged[k] = {"key": k, "content": item["content"]}
            else:
                # 同 key 已存在，保留向量侧内容
                pass
        # 后续按内容二次去重
        items = list(merged.values())
        # 无向量能力时退化为衰减排序
        if not self._vector_enabled or not vector_candidates:
            # 命名空间已在召回时过滤，直接走衰减排序
            _res_decay = self._rank_with_decay(bm25_candidates if bm25_candidates else items)
            try:
                if _sck is not None:
                    self._cache_set(self._retrieval_cache, _sck, _res_decay)
            except Exception:
                pass
            return _res_decay

        # 统一融合：RRF(k=60) + 归一 0.5*RRF + 0.5*cosine
        try:
            from hero_quant.memory.rank_fusion import rank_fusion as _rank_fusion
        except Exception:
            _rank_fusion = None  # type: ignore
        # 构建 rank_fusion 输入：bm25 按出现顺序赋分，vec 用真实 cosine
        # bm25 候选赋予递减分数以保留排序信息
        bm25_tuples: list[tuple[str, float]] = []
        for idx, it in enumerate(bm25_candidates):
            bm25_tuples.append((it["key"], float(len(bm25_candidates) - idx)))
        # vec 候选用 vector_search 已有 score，若无则即时计算 cosine
        vec_tuples: list[tuple[str, float]] = []
        if vector_candidates:
            for it in vector_candidates:
                sc = it.get("score", 0.0)
                try:
                    sc_f = float(sc)
                except Exception:
                    sc_f = 0.0
                vec_tuples.append((it["key"], sc_f))
        else:
            # 回退：为 items 即时计算 cosine 以喂入融合
            try:
                qvec = self._embed_text(query)
            except Exception:
                qvec = None
            if qvec is not None:
                for it in items:
                    try:
                        cvec = self._load_vector_for_key(it["key"])
                        if cvec is None:
                            cvec = self._embed_text(it["content"])
                        cos = self._cosine_sim(qvec, cvec) if cvec is not None else 0.0
                    except Exception:
                        cos = 0.0
                    if cos < 0:
                        cos = 0.0
                    if cos > 1:
                        cos = 1.0
                    vec_tuples.append((it["key"], cos))

        if _rank_fusion is not None and (bm25_tuples or vec_tuples):
            try:
                ranked = _rank_fusion(bm25_tuples, vec_tuples, k=60)
                # ranked is list[(key, hybrid)]
                # Map back to dict results preserving content
                out: list[dict] = []
                for key, _sc in ranked:
                    if key in merged:
                        out.append(merged[key])
                # Include any missing merged keys (未参与融合的) 尾部补齐
                seen_keys = {k for k, _ in ranked}
                for it in items:
                    if it["key"] not in seen_keys:
                        out.append(it)
                result = out
            except Exception:
                # fallback preserve previous order
                result = items
        else:
            # 极端回退：按原 items
            result = items

        # Cohere 重排增强（若配置 COHERE_API_KEY）
        try:
            from hero_quant.config.settings import Settings as _Settings

            _cohere_key = (_Settings().cohere_api_key or "").strip()
        except Exception:
            _cohere_key = ""
        if _cohere_key:
            try:
                from hero_quant.memory.rerank import CohereReranker as _Reranker

                reranker = _Reranker(api_key=_cohere_key, timeout=5)
                # Prepare candidates as (key, score) where score from rank_fusion
                # Use current result order as prior score
                cands_for_rerank: list[tuple[str, float]] = []
                for idx, it in enumerate(result):
                    cands_for_rerank.append((it["key"], float(len(result) - idx)))
                reranked = reranker.rerank(query, cands_for_rerank)
                if reranked:
                    # Map reranked order back to dict
                    reranked_keys = [k for k, _ in reranked]
                    map_content = {it["key"]: it for it in result}
                    # also fallback to merged for missing
                    for k, v in merged.items():
                        if k not in map_content:
                            map_content[k] = v
                    out2: list[dict] = []
                    for k in reranked_keys:
                        if k in map_content:
                            out2.append(map_content[k])
                    # append any not in reranked tail
                    for it in result:
                        if it["key"] not in reranked_keys:
                            out2.append(it)
                    result = out2
            except Exception as _exc:
                logger.debug("silent handled: rerank fallback", exc_info=_exc)  # intentional fallback
                pass

        # 已做命名空间隔离，最后按内容去重
        seen: dict[str, dict] = {}
        deduped: list[dict] = []
        for it in result:
            if it["content"] not in seen:
                seen[it["content"]] = it
                deduped.append(it)
        try:
            if _sck is not None:
                self._cache_set(self._retrieval_cache, _sck, deduped)
        except Exception:
            pass
        return deduped

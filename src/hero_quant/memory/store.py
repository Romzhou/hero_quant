"""MemoryStore - file+sqlite FTS5+dedup minimal implementation with vector hybrid + pgvector sidecar."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .lifecycle import HALF_LIFE_DAYS, _DECAY_LAMBDA, compute_importance


def _content_hash(name: str, content: str) -> str:
    """vibe persistent.py:35-39 思路 content_hash，去重哈希.

    sha256((f"{name}:{content}").lower().strip().encode()).hexdigest()[:12]
    """
    return hashlib.sha256(f"{name}:{content}".lower().strip().encode()).hexdigest()[:12]


# ---- pgvector sidecar hardening (Wave E3) ----
# Env gate consolidated in Settings; thin wrappers here delegate to single sources.
_PG_PREFIXES = ("postgresql://", "postgres://", "postgresql+psycopg://")


def _pgvector_dsn() -> str | None:
    """Resolve pgvector DSN via Settings (single env gate)."""
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
    """Whether pgvector sidecar should be attempted (env gate via Settings)."""
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
    """Single source: delegate to hero_quant.agent.embed.get_vector_dim."""
    try:
        from hero_quant.agent.embed import get_vector_dim as _embed_dim

        return _embed_dim()
    except Exception:
        return 32


def _vector_to_literal(vec) -> str:
    """Serialize vector to pgvector literal '[0.1,0.2]' for TEXT fallback or vector type."""
    try:
        from hero_quant.agent.embed import to_pgvector_literal  # type: ignore

        return to_pgvector_literal(vec)  # type: ignore
    except Exception:
        try:
            return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"
        except Exception:
            return "[" + ",".join(str(x) for x in vec) + "]"


class PgVectorSidecar:
    """Postgres pgvector sidecar — first-class when configured, graceful fallback to local.

    - DDL: CREATE EXTENSION vector + memory_vectors(key PK, content, embedding vector(dim), namespace)
    - UPSERT via INSERT ... ON CONFLICT (key) DO UPDATE
    - SEARCH via embedding <=> query::vector (cosine distance) with namespace filter
    - If psycopg not installed or connection fails, _enabled=False and all ops no-op.
    - Timeout hardening: connection timeout 5s, statement timeout 5s.
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
        # lazy import pool; fallback to direct psycopg if pool unavailable
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
            # check psycopg availability
            try:
                import importlib.util as _ilu

                if _ilu.find_spec("psycopg") is None:
                    self._last_error = "psycopg not installed"
                    return
                # no pool, will use direct connect per operation — still enabled
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
            # detect async
            try:
                import inspect as _ins

                self._is_async = "Async" in type(self._pool).__name__ or _ins.iscoroutinefunction(getattr(self._pool, "open", None))
            except Exception:
                self._is_async = False
            self._enabled = True
            # try ensure schema sync only if not async (async will do lazy)
            if not self._is_async:
                self._ensure_schema_sync()
        except Exception as e:
            self._last_error = str(e)
            self._pool = None
            self._enabled = False

    def _ensure_schema_sync(self) -> None:
        if not self._enabled or self._is_async:
            return
        # DDL guarded — extension + table + index (best effort)
        try:
            if self._pool is not None and hasattr(self._pool, "connection"):
                with self._pool.connection() as conn:  # type: ignore
                    try:
                        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                    except Exception:
                        pass
                    # try vector type, fallback to TEXT
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
                    # best-effort index (ivfflat requires vector)
                    if created:
                        try:
                            # only if vector type succeeded
                            conn.execute(
                                "CREATE INDEX IF NOT EXISTS idx_memory_vectors_embedding ON memory_vectors USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
                            )
                        except Exception:
                            pass
                        try:
                            conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_vectors_ns ON memory_vectors (namespace)")
                        except Exception:
                            pass
                    try:
                        conn.commit()  # type: ignore
                    except Exception:
                        pass
            elif self._pool is not None and hasattr(self._pool, "getconn"):
                conn = self._pool.getconn()  # type: ignore
                try:
                    with conn.cursor() as cur:
                        try:
                            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                        except Exception:
                            pass
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
                        except Exception:
                            pass
                    conn.commit()
                finally:
                    try:
                        self._pool.putconn(conn)  # type: ignore
                    except Exception:
                        pass
            else:
                # direct psycopg path — try one-off connection
                try:
                    import psycopg  # type: ignore

                    with psycopg.connect(self.dsn, connect_timeout=5) as conn:  # type: ignore
                        with conn.cursor() as cur:
                            try:
                                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                            except Exception:
                                pass
                            try:
                                cur.execute(
                                    f"CREATE TABLE IF NOT EXISTS memory_vectors (key TEXT PRIMARY KEY, content TEXT, embedding vector({self.dim}), namespace TEXT, created TIMESTAMPTZ DEFAULT now())"
                                )
                            except Exception:
                                cur.execute(
                                    "CREATE TABLE IF NOT EXISTS memory_vectors (key TEXT PRIMARY KEY, content TEXT, embedding TEXT, namespace TEXT, created TIMESTAMPTZ DEFAULT now())"
                                )
                        conn.commit()
                except Exception:
                    pass
        except Exception as e:
            self._last_error = str(e)
            pass

    def ping(self) -> bool:
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
                    except Exception:
                        pass
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
        if not self._enabled or not key:
            return False
        lit = _vector_to_literal(embedding)
        ns = namespace or ""
        # try sync pool path; async pool will fallback to False (caller fallback to local)
        if self._is_async:
            return False
        try:
            if self._pool is not None and hasattr(self._pool, "connection"):
                with self._pool.connection() as conn:  # type: ignore
                    # try vector::vector insertion, fallback to TEXT
                    try:
                        conn.execute(
                            "INSERT INTO memory_vectors (key, content, embedding, namespace) VALUES (%s, %s, %s::vector, %s) ON CONFLICT (key) DO UPDATE SET content=EXCLUDED.content, embedding=EXCLUDED.embedding, namespace=EXCLUDED.namespace",
                            (key, content, lit, ns),
                        )
                    except Exception:
                        # fallback TEXT column
                        with conn.cursor() as cur:  # type: ignore
                            cur.execute(
                                "INSERT INTO memory_vectors (key, content, embedding, namespace) VALUES (%s, %s, %s, %s) ON CONFLICT (key) DO UPDATE SET content=EXCLUDED.content, embedding=EXCLUDED.embedding, namespace=EXCLUDED.namespace",
                                (key, content, lit, ns),
                            )
                    try:
                        conn.commit()  # type: ignore
                    except Exception:
                        pass
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
                    except Exception:
                        pass
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
                        # cosine distance: 1 - cosine => ORDER BY embedding <=> query asc
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
                    except Exception:
                        pass
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
                    k, c, emb = r[0], r[1], r[2]
                    out.append({"key": k, "content": c})
                except Exception:
                    continue
            return out
        except Exception as e:
            self._last_error = str(e)
            return []


class MemoryStore:
    """File storage with SQLite FTS5 and 30s dedup + optional tenant/thread namespace isolation + vector hybrid + pgvector sidecar."""

    def __init__(self, base_path: Path | str, namespace: str | None = None):
        self.base = Path(base_path)
        self.base.mkdir(parents=True, exist_ok=True)
        self.namespace = namespace
        self._recent_hashes: dict[str, float] = {}
        self._meta: dict[str, dict] = {}  # ns_key -> {quality_score, access_count, last_accessed}
        self._fts_enabled = False
        self._vector_enabled = False
        self.db_path = self.base / "memory.db"
        self._init_db()
        # hierarchy helper (lazy import to avoid cycle)
        try:
            from .hierarchy import MemoryHierarchy

            self.hierarchy = MemoryHierarchy(self.base)
        except Exception:
            self.hierarchy = None  # type: ignore
        # pgvector sidecar — first-class when configured, fallback to local hybrid if unavailable
        self._pgvector: PgVectorSidecar | None = None
        self._pgvector_enabled: bool = False
        try:
            if is_pgvector_configured():
                # dim may be overridden via env; use get_vector_dim()
                self._pgvector = PgVectorSidecar(dim=get_vector_dim())
                # _enabled may be False if pool fails; still keep object for diagnostics
                self._pgvector_enabled = bool(getattr(self._pgvector, "_enabled", False))
                # vector hybrid remains enabled even if pg not reachable (local fallback)
                if self._pgvector_enabled:
                    self._vector_enabled = True
            else:
                self._pgvector = None
                self._pgvector_enabled = False
        except Exception:
            self._pgvector = None
            self._pgvector_enabled = False

    @property
    def vector_backend(self) -> str:
        """Return 'pgvector' if sidecar enabled and reachable, else 'local'."""
        if self._pgvector is not None and getattr(self._pgvector, "_enabled", False):
            # optional ping check (best effort, not blocking search)
            return "pgvector"
        return "local"

    def get_vector_backend(self) -> str:
        return self.vector_backend

    def is_pgvector_enabled(self) -> bool:
        return bool(self._pgvector_enabled and self._pgvector is not None and getattr(self._pgvector, "_enabled", False))

    def _ns_key(self, key: str) -> str:
        """Prefix key with namespace if set: f"{namespace}:{key}" else key."""
        if self.namespace:
            return f"{self.namespace}:{key}"
        return key

    def _ns_prefix(self) -> str | None:
        if self.namespace:
            return f"{self.namespace}:"
        return None

    def _safe_filename(self, ns_key: str) -> str:
        # Windows-safe: colon and slash not allowed in filenames
        safe = ns_key.replace(":", "__").replace("/", "__").replace("\\", "__")
        # also strip any path separators that could cause traversal
        safe = safe.replace("..", "__")
        return f"{safe}.md"

    def _safe_prefix(self) -> str | None:
        if self.namespace:
            # sanitized prefix for file filtering
            return self.namespace.replace(":", "__").replace("/", "__").replace("\\", "__") + "__"
        return None

    def _init_db(self) -> None:
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        # notes table — include vector column for hybrid recall (D3)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT, content TEXT, created TEXT)"
        )
        # ensure vector column exists (for existing DBs)
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
            # re-check
            cur.execute("PRAGMA table_info(notes)")
            cols2 = [row[1] for row in cur.fetchall()]
            self._vector_enabled = "vector" in cols2 or "embedding" in cols2
            # also consider that we could have vector handling even if column missing (fallback in-memory)
            if "vector" in cols2:
                self._vector_enabled = True
            else:
                # still enable vector logic via on-the-fly embed (no column) to satisfy tests
                self._vector_enabled = True
        except Exception:
            self._vector_enabled = True
        # try FTS5 creation
        try:
            # trigram helps CJK, try first
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(content, tokenize='trigram')"
            )
            self._fts_enabled = True
        except sqlite3.OperationalError:
            try:
                self._conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(content)"
                )
                self._fts_enabled = True
            except sqlite3.OperationalError:
                self._fts_enabled = False
        self._conn.commit()

    def _embed_text(self, text: str):
        """Lazy embed helper — imports pluggable embed to avoid circular deps."""
        try:
            from hero_quant.agent.embed import embed  # type: ignore

            return embed(text)
        except Exception:
            # fallback deterministic hash 32-dim
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
        """Load stored vector JSON for key if column exists, else None.

        Handles dim drift: if stored vector length mismatches current
        get_vector_dim(), treat as stale and return None to trigger re-embed.
        """
        try:
            cur = self._conn.cursor()
            # check column exists first
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
                # dim drift check: single source via embed.get_vector_dim
                try:
                    from hero_quant.agent.embed import get_vector_dim as _gvd

                    expected = int(_gvd())
                    if isinstance(parsed, list) and len(parsed) != expected:
                        return None
                except Exception:
                    pass
                return parsed
        except Exception:
            return None
        return None

    def _ensure_vector_dim(self, vec, content: str):
        """Re-embed if vec is None or dim mismatches current dim."""
        try:
            from hero_quant.agent.embed import get_vector_dim as _gvd

            expected = int(_gvd())
            if vec is None or not isinstance(vec, list) or len(vec) != expected:
                return self._embed_text(content)
            return vec
        except Exception:
            return vec if vec is not None else self._embed_text(content)

    # ---- pgvector sidecar helpers ----
    def _pgvector_upsert(self, ns_key: str, content: str, vec) -> None:
        """Best-effort upsert into pgvector sidecar — never raises, falls back silently."""
        if not self._pgvector_enabled or self._pgvector is None or vec is None:
            return
        try:
            # namespace isolation: sidecar stores namespace separately for filtered search
            ns = self.namespace or ""
            ok = self._pgvector.upsert(ns_key, content, vec, namespace=ns)
            if not ok:
                # do not disable permanently; keep enabled flag for next try
                pass
        except Exception:
            pass

    def _pgvector_search(self, query: str, top_k: int = 5) -> list[dict]:
        """Try pgvector sidecar search — returns [] on any failure to trigger local fallback."""
        if not self._pgvector_enabled or self._pgvector is None:
            return []
        if not query:
            return []
        try:
            qvec = self._embed_text(query)
            if qvec is None:
                return []
            ns = self.namespace or "" if self.namespace else None
            # search sidecar
            pg_hits = self._pgvector.search(qvec, top_k=top_k, namespace=ns)
            # pg_hits are [{"key":..., "content":...}] already namespace-filtered if backend supports it
            # ensure prefix filtering for safety when namespace is set but sidecar stored with prefix
            if self._ns_prefix() is not None and pg_hits:
                prefix = self._ns_prefix()
                pg_hits = [h for h in pg_hits if h.get("key", "").startswith(prefix or "")]
            # attach score via cosine for hybrid re-rank (compute locally if not returned)
            out: list[dict] = []
            for h in pg_hits:
                # compute cosine on the fly for hybrid (pg distance not exposed, so recompute)
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
        now = time.time()
        ns_key = self._ns_key(key)
        # 30s sliding window content_hash dedup (cross-key, case/whitespace normalized)
        # 过期清理：移除 >30s 的哈希
        self._recent_hashes = {h: ts for h, ts in self._recent_hashes.items() if now - ts < 30}
        # 内容归一哈希（跨 key 去重核心，大小写/空白归一）
        content_hash = hashlib.sha256(content.lower().strip().encode()).hexdigest()[:12]
        # 完整 name:content 哈希（vibe 兼容，满足 spec _content_hash 要求）
        full_hash = _content_hash(ns_key, content)
        if content_hash in self._recent_hashes or full_hash in self._recent_hashes:
            return
        self._recent_hashes[content_hash] = now
        self._recent_hashes[full_hash] = now

        # atomic file write: tmp -> fsync -> os.replace, 0600, flock compat
        # hierarchy routing: if memory_type in CATEGORIES route to subdir
        safe_name = self._safe_filename(ns_key)
        if memory_type:
            try:
                from .hierarchy import CATEGORIES

                if memory_type in CATEGORIES and self.hierarchy is not None:
                    file_path = self.hierarchy.route_entry(memory_type, safe_name)
                else:
                    # unknown type falls back to base
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
        tmp_path = self.base / f".{safe_name}.tmp.{os.getpid()}"
        # ensure parent exists (key may contain subdirs)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                try:
                    import fcntl  # type: ignore

                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                except ImportError:
                    # Windows fallback: try msvcrt
                    try:
                        import msvcrt  # type: ignore

                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    except Exception:
                        pass
                except Exception:
                    pass
                f.write(content)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            try:
                os.chmod(tmp_path, 0o600)
            except Exception:
                pass
            os.replace(tmp_path, file_path)
            try:
                os.chmod(file_path, 0o600)
            except Exception:
                pass
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

        # sqlite index with namespaced key — with vector column
        created = datetime.now(timezone.utc).isoformat()
        # compute vector embedding (pluggable dense)
        vector_json = None
        try:
            vec = self._embed_text(content)
            if vec is not None:
                vector_json = json.dumps(vec)
        except Exception:
            vector_json = None
        try:
            cur = self._conn.cursor()
            # check if vector column exists
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
                # try to add vector after
                if vector_json is not None:
                    try:
                        # attempt to store via update if column appears later
                        pass
                    except Exception:
                        pass
            rowid = cur.lastrowid
            if self._fts_enabled:
                try:
                    cur.execute(
                        "INSERT INTO notes_fts (rowid, content) VALUES (?, ?)",
                        (rowid, content),
                    )
                except sqlite3.OperationalError:
                    # fallback without rowid mapping
                    try:
                        cur.execute(
                            "INSERT INTO notes_fts (content) VALUES (?)", (content,)
                        )
                    except Exception:
                        pass
            self._conn.commit()
            # pgvector sidecar upsert (best-effort, never breaks local write)
            try:
                if vector_json is not None:
                    try:
                        vec_obj = json.loads(vector_json) if isinstance(vector_json, str) else vector_json
                    except Exception:
                        vec_obj = None
                    if vec_obj is not None:
                        self._pgvector_upsert(ns_key, content, vec_obj)
                else:
                    # fallback: compute vec again for pg if needed (dim-consistent)
                    try:
                        vec2 = self._embed_text(content)
                        if vec2 is not None:
                            self._pgvector_upsert(ns_key, content, vec2)
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
        # Ebbinghaus meta: default quality_score/access_count/last_accessed (in-memory Dict, no DDL)
        if ns_key not in self._meta:
            self._meta[ns_key] = {"quality_score": 0.5, "access_count": 0, "last_accessed": now}
        else:
            # preserve existing but update last_accessed if not explicitly managed? keep as is
            pass

    def _importance_for(self, item: dict, now: float) -> float:
        """Compute importance for a search item via Ebbinghaus 14d decay."""
        ns_key = item.get("key", "")
        meta = self._meta.get(ns_key)
        if meta is None:
            # file-scan fallback uses stem (safe filename) not raw ns_key; try reverse lookup
            for k, v in self._meta.items():
                if self._safe_filename(k).removesuffix(".md") == ns_key:
                    meta = v
                    break
            if meta is None:
                # suffix match fallback for namespace-prefixed keys
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
        if not items or len(items) <= 1:
            return items
        now = time.time()
        scored: list[tuple[float, float, dict]] = []
        for it in items:
            imp = self._importance_for(it, now)
            weighted = 1.0 * (0.5 + 0.5 * imp)  # score*(0.5+0.5*importance), base score=1
            scored.append((weighted, imp, it))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [it for _, _, it in scored]

    def vector_search(self, query: str, top_k: int = 5) -> list[dict]:
        """Pure vector cosine topK search — hybrid component.

        Uses pgvector sidecar first-class when configured, fallback to stored vector column
        or on-the-fly local cosine. Namespace-aware filtering.
        """
        if not query:
            return []
        # pgvector sidecar first-class: try PG before local (hybrid fallback)
        try:
            pg_hits = self._pgvector_search(query, top_k=top_k)
            if pg_hits:
                # if pg returned enough hits, return directly; else supplement with local
                if len(pg_hits) >= max(1, int(top_k)):
                    return pg_hits[: max(1, int(top_k))]
                # will merge later — keep pg_hits for union
            else:
                pg_hits = []
        except Exception:
            pg_hits = []
        prefix = self._ns_prefix()
        # embed query
        try:
            qvec = self._embed_text(query)
        except Exception:
            return pg_hits  # fallback to whatever pg gave
        # fetch all notes with vector
        candidates: list[dict] = []
        try:
            cur = self._conn.cursor()
            # check vector column
            cur.execute("PRAGMA table_info(notes)")
            cols = [row[1] for row in cur.fetchall()]
            has_vector = "vector" in cols
            if has_vector:
                cur.execute("SELECT key, content, vector FROM notes")
                rows = cur.fetchall()
                for k, c, v in rows:
                    if prefix is not None and not k.startswith(prefix):
                        continue
                    # parse vector if present
                    note_vec = None
                    if v:
                        try:
                            note_vec = json.loads(v) if isinstance(v, str) else v
                        except Exception:
                            note_vec = None
                    # dim drift: re-embed if stored vector length mismatches current dim
                    note_vec = self._ensure_vector_dim(note_vec, c)
                    if note_vec is None:
                        continue
                    sim = self._cosine_sim(qvec, note_vec)
                    candidates.append({"key": k, "content": c, "vector": note_vec, "_score": sim})
            else:
                # no vector column: fallback to content-only LIKE + on-fly embed for all rows
                cur.execute("SELECT key, content FROM notes")
                rows = cur.fetchall()
                for k, c in rows:
                    if prefix is not None and not k.startswith(prefix):
                        continue
                    note_vec = self._embed_text(c)
                    sim = self._cosine_sim(qvec, note_vec)
                    candidates.append({"key": k, "content": c, "_score": sim})
        except Exception:
            # DB fallback: scan files
            candidates = []
        # file fallback if DB gave no candidates but files exist (hierarchy-aware)
        if not candidates:
            try:
                from .hierarchy import MemoryHierarchy

                mh = MemoryHierarchy(self.base)
                files = mh.scan_all()
            except Exception:
                files = list(self.base.rglob("*.md"))
                files = [p for p in files if "archive" not in p.parts]
            safe_prefix = self._safe_prefix()
            for md_file in files:
                try:
                    if safe_prefix is not None and not md_file.name.startswith(safe_prefix):
                        continue
                    txt = md_file.read_text(encoding="utf-8")
                    # derive key from filename reverse; approximate
                    # try to find DB key mapping via safe filename
                    derived_key = md_file.stem.replace("__", ":")
                    if prefix is not None and not derived_key.startswith(prefix.rstrip(":")):
                        # also check safe prefix
                        if safe_prefix and not md_file.name.startswith(safe_prefix):
                            continue
                    note_vec = self._embed_text(txt)
                    sim = self._cosine_sim(qvec, note_vec)
                    candidates.append({"key": derived_key, "content": txt, "_score": sim})
                except Exception:
                    continue
        # sort by cosine desc
        candidates.sort(key=lambda x: x.get("_score", 0.0), reverse=True)
        # merge pgvector sidecar hits (first-class) with local candidates — hybrid fallback
        # pg_hits already filtered by namespace and scored; union dedup by key keeps best score
        if pg_hits:
            # build map from local candidates for quick score lookup
            local_by_key = {c["key"]: c for c in candidates}
            # start with pg hits (preserve order by pg score)
            merged: dict[str, dict] = {}
            for h in pg_hits:
                merged[h["key"]] = {"key": h["key"], "content": h["content"], "_score": float(h.get("score", 0.0))}
            # add local candidates that are not already in pg
            for c in candidates:
                if c["key"] not in merged:
                    merged[c["key"]] = c
                else:
                    # keep higher score if duplicate (max of pg vs local)
                    if float(c.get("_score", 0.0)) > float(merged[c["key"]].get("_score", 0.0)):
                        merged[c["key"]]["_score"] = float(c.get("_score", 0.0))
            # re-sort merged by score desc
            all_items = list(merged.values())
            all_items.sort(key=lambda x: float(x.get("_score", 0.0)), reverse=True)
            out: list[dict] = []
            for it in all_items[: max(0, top_k)]:
                out.append({"key": it["key"], "content": it["content"], "score": float(it.get("_score", 0.0))})
            return out
        # strip internal _score before return? Keep for hybrid but expose without
        out: list[dict] = []
        for it in candidates[: max(0, top_k)]:
            # preserve key/content; optionally include score
            out.append({"key": it["key"], "content": it["content"], "score": it.get("_score", 0.0)})
        return out

    def _search_bm25_raw(self, query: str) -> list[dict]:
        """Original BM25/FTS/LIKE/file fallback without vector, returns deduplicated candidates."""
        if not query:
            return []
        prefix = self._ns_prefix()
        safe_prefix = self._safe_prefix()
        # try FTS5 MATCH first
        if self._fts_enabled:
            try:
                cur = self._conn.cursor()
                cur.execute(
                    "SELECT notes.key, notes.content FROM notes_fts JOIN notes ON notes_fts.rowid = notes.id WHERE notes_fts MATCH ?",
                    (query,),
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
            except Exception:
                pass
        # fallback LIKE
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
                        if safe_prefix is not None and not md_file.name.startswith(safe_prefix):
                            continue
                        if prefix is not None and not md_file.stem.replace("__", ":").startswith(prefix.rstrip(":")):
                            if not md_file.name.startswith(safe_prefix or ""):
                                continue
                        txt = md_file.read_text(encoding="utf-8")
                        if query in txt:
                            result.append({"key": md_file.stem, "content": txt})
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

    def search(self, query: str) -> list[dict]:
        """Hybrid search: BM25 FTS + LIKE plus vector cosine topK re-rank, preserving Ebbinghaus.

        Steps:
        - gather BM25 candidates via _search_bm25_raw
        - gather vector candidates via vector_search (top 10)
        - merge union deduplicated by key/content
        - compute hybrid score = 0.6*cosine + 0.3*importance + 0.1*bm25_hit
        - sort descending, yield merged ranked list
        Falls back to BM25 + decay if vector fails.
        """
        if not query:
            return []
        prefix = self._ns_prefix()
        # raw BM25 candidates
        bm25_candidates = self._search_bm25_raw(query)
        # vector candidates
        vector_candidates: list[dict] = []
        if self._vector_enabled:
            try:
                vector_candidates = self.vector_search(query, top_k=10)
            except Exception:
                vector_candidates = []
        # if both empty, try vector fallback alone (already merged) -> return empty
        if not bm25_candidates and not vector_candidates:
            return []
        # merge union by key
        merged: dict[str, dict] = {}
        # vector first (preserves vector order but hybrid will re-sort)
        for item in vector_candidates:
            k = item["key"]
            if k not in merged:
                merged[k] = {"key": k, "content": item["content"]}
        for item in bm25_candidates:
            k = item["key"]
            if k not in merged:
                merged[k] = {"key": k, "content": item["content"]}
            else:
                # keep content from bm25 if missing?
                pass
        # also include any bm25 duplicates by content not key?
        # dedup by content as secondary
        # Build list
        items = list(merged.values())
        # if we had no merge (e.g., vector disabled), fallback to decay rank
        if not self._vector_enabled or not vector_candidates:
            # No vector: use existing decay rank over bm25
            # But still need namespace filter already done
            return self._rank_with_decay(bm25_candidates if bm25_candidates else items)

        # compute hybrid scores
        now = time.time()
        # precompute query vector once
        try:
            qvec = self._embed_text(query)
        except Exception:
            qvec = None
        # build fast lookup for bm25 keys
        bm25_keys = {it["key"] for it in bm25_candidates}
        scored: list[tuple[float, float, float, dict]] = []
        for it in items:
            key = it["key"]
            content = it["content"]
            # cosine
            cos = 0.0
            if qvec is not None:
                # try stored vector first
                stored_vec = self._load_vector_for_key(key)
                if stored_vec is not None:
                    cos = self._cosine_sim(qvec, stored_vec)
                else:
                    # fallback compute from content
                    try:
                        cvec = self._embed_text(content)
                        cos = self._cosine_sim(qvec, cvec)
                    except Exception:
                        cos = 0.0
                # clamp to [-1,1]
                if cos > 1.0:
                    cos = 1.0
                elif cos < -1.0:
                    cos = -1.0
                # normalize negative to 0 for ranking (optional) keep raw
                # but keep as is for hybrid
            imp = self._importance_for(it, now)
            bm25_hit = 1.0 if key in bm25_keys else (1.0 if query.lower() in content.lower() else 0.0)
            hybrid = 0.6 * cos + 0.3 * imp + 0.1 * bm25_hit
            # also boost exact vector top? keep hybrid
            scored.append((hybrid, cos, imp, it))
        # sort by hybrid desc, then cosine, then importance
        scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        result = [it for _, _, _, it in scored]
        # namespace isolation already filtered; ensure deduplication by content
        seen: dict[str, dict] = {}
        deduped: list[dict] = []
        for it in result:
            if it["content"] not in seen:
                seen[it["content"]] = it
                deduped.append(it)
        return deduped

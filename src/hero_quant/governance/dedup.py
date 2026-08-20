"""DedupStore — tool_call_dedup idempotency ledger.

Table: tool_call_dedup(idempotency_key PK, status PENDING|SUCCESS|FAILED, tool, result, error)
INSERT ON CONFLICT WAIT placeholder (single-process check + retry).
Key derived as {tenant}:{workflowId}:{stepId}:{tool}:{businessId} at orchestration layer.

Hardened:
- PG branch: CREATE TABLE IF NOT EXISTS dedup (key TEXT PRIMARY KEY, tool TEXT, status TEXT, result JSONB, updated_at TIMESTAMPTZ) + TTL
- Single-process dict fallback when PG unavailable (offline synthetic)
- SQLite file path preserved for backward compat / single-process tests
"""

from __future__ import annotations

import inspect
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

DEFAULT_TTL_SECONDS = 7 * 24 * 3600

_PG_PREFIXES = ("postgresql://", "postgres://", "postgresql+psycopg://")

DDL_DEDUP_PG = """
CREATE TABLE IF NOT EXISTS dedup (
  key TEXT PRIMARY KEY,
  tool TEXT,
  status TEXT,
  result JSONB,
  updated_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_dedup_status ON dedup (status);
CREATE INDEX IF NOT EXISTS idx_dedup_updated_at ON dedup (updated_at);
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


def derive_key(tenant: str, workflow_id: str, step_id: str, tool: str, business_id: str) -> str:
    """Derive idempotency key at orchestration layer."""
    parts = [tenant, workflow_id, step_id, tool, business_id]
    return ":".join(str(p) for p in parts)


class DedupStore:
    """SQLite-backed idempotency ledger with PG branch + in-memory dict fallback."""

    def __init__(self, db_path: str | Path = "memory://dedup", *, ttl_seconds: int | None = None, dsn: str | None = None):
        # ttl
        self.ttl_seconds = int(ttl_seconds) if ttl_seconds is not None else DEFAULT_TTL_SECONDS
        # in-memory dict fallback (single-process)
        self._mem: dict[str, dict[str, Any]] = {}
        self._mem_ts: dict[str, float] = {}

        # detect PG vs SQLite vs memory dict
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
            # PG branch
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
            # SQLite / memory dict path
            if raw.startswith("memory://"):
                # pure memory dict (no sqlite file) — map to dict only
                self.db_path = None
            elif raw in ("", ":memory:"):
                self.db_path = None
            else:
                self.db_path = Path(raw) if raw else Path("dedup.db")
                # ensure parent exists only if file path
                try:
                    self.db_path.parent.mkdir(parents=True, exist_ok=True)
                except Exception:
                    self.db_path = None
                if self.db_path is not None:
                    self._init_db()

    # ---- PG setup ----
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
                        conn.commit()  # type: ignore
                    except Exception:
                        pass
            elif hasattr(self.pool, "getconn"):
                conn = self.pool.getconn()  # type: ignore
                try:
                    with conn.cursor() as cur:
                        cur.execute(DDL_DEDUP_PG)
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
            except Exception:
                try:
                    async with self.pool.connection() as conn:  # type: ignore
                        async with conn.cursor() as cur:  # type: ignore
                            await cur.execute(DDL_DEDUP_PG)
                except Exception:
                    pass

    # ---- SQLite ----
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

    # ---- memory helpers ----
    def _mem_is_expired(self, key: str) -> bool:
        ts = self._mem_ts.get(key)
        if ts is None or self.ttl_seconds <= 0:
            return False
        return (time.time() - ts) > self.ttl_seconds

    def _mem_cleanup(self, key: str) -> None:
        if self._mem_is_expired(key):
            self._mem.pop(key, None)
            self._mem_ts.pop(key, None)

    def _mem_get(self, key: str) -> dict[str, Any] | None:
        self._mem_cleanup(key)
        rec = self._mem.get(key)
        if rec is None:
            return None
        # deep copy to avoid mutation
        return dict(rec)

    # ---- PG helpers (sync) ----
    def _pg_insert_pending_sync(self, key: str, tool: str) -> bool | None:
        """Try PG INSERT ON CONFLICT DO NOTHING. Returns True/False if PG succeeded, None if fallback."""
        if not self._is_pg or self.pool is None or _is_async_pool(self.pool):
            return None
        try:
            # cleanup expired first: delete where updated_at < now() - ttl
            if self.ttl_seconds > 0:
                # we skip ttl delete for now to keep SQL simple; expiry handled in SELECT
                pass
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
                    try:
                        cur = conn.execute(sql, (key, tool))  # type: ignore
                        # rowcount 1 if inserted
                        inserted = getattr(cur, "rowcount", 0) == 1
                    except Exception:
                        with conn.cursor() as cur2:  # type: ignore
                            cur2.execute(sql, (key, tool))
                            inserted = getattr(cur2, "rowcount", 0) == 1
                    # also try alias table for compat (ignore errors)
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
        except Exception:
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
        except Exception:
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
        except Exception:
            return False

    # ---- public API ----
    def insert_pending(self, key: str, tool: str) -> bool:
        """INSERT ON CONFLICT WAIT placeholder — returns True if inserted, False if exists."""
        now = time.time()
        # PG branch first when available
        if self._is_pg:
            pg_res = self._pg_insert_pending_sync(key, tool)
            if pg_res is not None:
                # keep mem in sync for fallback reads
                if pg_res:
                    self._mem[key] = {"key": key, "tool": tool, "status": "PENDING", "result": None, "updated_at": now}
                    self._mem_ts[key] = now
                else:
                    # existing: ensure mem reflects PG state if not yet
                    if key not in self._mem:
                        pg_rec = self._pg_get_sync(key)
                        if pg_rec:
                            self._mem[key] = pg_rec
                            self._mem_ts[key] = now
                return bool(pg_res)
            # PG unavailable -> fallback to mem/SQLite below

        # SQLite path
        if self.db_path is not None:
            con = self._connect()
            try:
                # TTL-aware: if expired, allow re-insert by deleting expired row first
                if self.ttl_seconds > 0:
                    try:
                        con.execute("DELETE FROM tool_call_dedup WHERE idempotency_key=? AND updated_at < ?", (key, now - self.ttl_seconds))
                    except Exception:
                        pass
                cur = con.execute("SELECT status FROM tool_call_dedup WHERE idempotency_key=?", (key,))
                row = cur.fetchone()
                if row is not None:
                    # also update mem for consistency
                    self._mem[key] = {"key": key, "tool": tool, "status": row[0], "result": None, "updated_at": now}
                    self._mem_ts[key] = now
                    return False
                try:
                    con.execute(
                        "INSERT INTO tool_call_dedup (idempotency_key, status, tool, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (key, "PENDING", tool, now, now),
                    )
                    self._mem[key] = {"key": key, "tool": tool, "status": "PENDING", "result": None, "updated_at": now}
                    self._mem_ts[key] = now
                    return True
                except sqlite3.IntegrityError:
                    return False
            finally:
                con.close()
        # pure memory dict fallback (single-process)
        self._mem_cleanup(key)
        if key in self._mem:
            return False
        self._mem[key] = {"key": key, "tool": tool, "status": "PENDING", "result": None, "updated_at": now}
        self._mem_ts[key] = now
        return True

    def mark_success(self, key: str, result: Any) -> None:
        now = time.time()
        result_json = json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
        # PG branch
        if self._is_pg:
            if self._pg_mark_sync(key, "SUCCESS", result=result, error=None):
                self._mem[key] = {"key": key, "tool": self._mem.get(key, {}).get("tool"), "status": "SUCCESS", "result": result, "updated_at": now}
                self._mem_ts[key] = now
                # also try sqlite alias if exists? not needed for PG
                return
            # fallback to mem/sqlite on PG failure
        if self.db_path is not None:
            con = self._connect()
            try:
                con.execute(
                    "UPDATE tool_call_dedup SET status=?, result=?, updated_at=? WHERE idempotency_key=?",
                    ("SUCCESS", result_json, now, key),
                )
                if con.total_changes == 0:
                    con.execute(
                        "INSERT OR IGNORE INTO tool_call_dedup (idempotency_key, status, result, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (key, "SUCCESS", result_json, now, now),
                    )
            finally:
                con.close()
        # mem sync
        rec = self._mem.get(key, {})
        self._mem[key] = {"key": key, "tool": rec.get("tool"), "status": "SUCCESS", "result": result, "updated_at": now}
        self._mem_ts[key] = now

    def mark_failed(self, key: str, error: str) -> None:
        now = time.time()
        if self._is_pg:
            if self._pg_mark_sync(key, "FAILED", result=None, error=error):
                self._mem[key] = {"key": key, "tool": self._mem.get(key, {}).get("tool"), "status": "FAILED", "error": str(error), "updated_at": now}
                self._mem_ts[key] = now
                return
        if self.db_path is not None:
            con = self._connect()
            try:
                con.execute(
                    "UPDATE tool_call_dedup SET status=?, error=?, updated_at=? WHERE idempotency_key=?",
                    ("FAILED", str(error), now, key),
                )
                if con.total_changes == 0:
                    con.execute(
                        "INSERT OR IGNORE INTO tool_call_dedup (idempotency_key, status, error, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (key, "FAILED", str(error), now, now),
                    )
            finally:
                con.close()
        rec = self._mem.get(key, {})
        self._mem[key] = {"key": key, "tool": rec.get("tool"), "status": "FAILED", "error": str(error), "updated_at": now}
        self._mem_ts[key] = now

    def get(self, key: str) -> dict[str, Any] | None:
        # PG branch first (with TTL via PG query)
        if self._is_pg:
            pg_rec = self._pg_get_sync(key)
            if pg_rec is not None:
                # keep mem warm
                self._mem[key] = pg_rec
                self._mem_ts[key] = time.time()
                # normalize result JSON
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
                    # sync mem
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

    def wait_for(self, key: str, timeout: float = 5.0) -> dict[str, Any] | None:
        """Polling WAIT placeholder for ON CONFLICT WAIT semantics."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            rec = self.get(key)
            if rec is not None and rec.get("status") in ("SUCCESS", "FAILED"):
                return rec
            # PG branch additional WAIT using SELECT FOR UPDATE? Polling is fallback
            time.sleep(0.05)
        return self.get(key)

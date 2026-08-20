"""DedupStore — tool_call_dedup idempotency ledger.

Table: tool_call_dedup(idempotency_key PK, status PENDING|SUCCESS|FAILED, tool, result, error)
INSERT ON CONFLICT WAIT placeholder (single-process check + retry).
Key derived as {tenant}:{workflowId}:{stepId}:{tool}:{businessId} at orchestration layer.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


def derive_key(tenant: str, workflow_id: str, step_id: str, tool: str, business_id: str) -> str:
    """Derive idempotency key at orchestration layer."""
    parts = [tenant, workflow_id, step_id, tool, business_id]
    return ":".join(str(p) for p in parts)


class DedupStore:
    """SQLite-backed idempotency ledger."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        return con

    def _init_db(self) -> None:
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
            # Ensure index exists (PK already)
            con.execute("CREATE INDEX IF NOT EXISTS idx_tool_status ON tool_call_dedup(status)")
        finally:
            con.close()

    def insert_pending(self, key: str, tool: str) -> bool:
        """INSERT ON CONFLICT WAIT placeholder — returns True if inserted, False if exists."""
        now = time.time()
        con = self._connect()
        try:
            # Check existing first (WAIT placeholder — poll once)
            cur = con.execute("SELECT status FROM tool_call_dedup WHERE idempotency_key=?", (key,))
            row = cur.fetchone()
            if row is not None:
                return False
            try:
                con.execute(
                    "INSERT INTO tool_call_dedup (idempotency_key, status, tool, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (key, "PENDING", tool, now, now),
                )
                return True
            except sqlite3.IntegrityError:
                # Race — another inserter won
                return False
        finally:
            con.close()

    def mark_success(self, key: str, result: Any) -> None:
        now = time.time()
        result_json = json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
        con = self._connect()
        try:
            con.execute(
                "UPDATE tool_call_dedup SET status=?, result=?, updated_at=? WHERE idempotency_key=?",
                ("SUCCESS", result_json, now, key),
            )
            # If key didn't exist, insert as SUCCESS (upsert fallback)
            if con.total_changes == 0:
                con.execute(
                    "INSERT OR IGNORE INTO tool_call_dedup (idempotency_key, status, result, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (key, "SUCCESS", result_json, now, now),
                )
        finally:
            con.close()

    def mark_failed(self, key: str, error: str) -> None:
        now = time.time()
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

    def get(self, key: str) -> dict[str, Any] | None:
        con = self._connect()
        try:
            cur = con.execute(
                "SELECT idempotency_key, status, tool, result, error, created_at, updated_at FROM tool_call_dedup WHERE idempotency_key=?",
                (key,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            col_names = [d[0] for d in cur.description]
            rec = dict(zip(col_names, row))
            # Parse result JSON if looks like JSON
            if rec.get("result") is not None:
                try:
                    rec["result"] = json.loads(rec["result"])
                except Exception:
                    pass
            return rec
        finally:
            con.close()

    def wait_for(self, key: str, timeout: float = 5.0) -> dict[str, Any] | None:
        """Polling WAIT placeholder for ON CONFLICT WAIT semantics."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            rec = self.get(key)
            if rec is not None and rec.get("status") in ("SUCCESS", "FAILED"):
                return rec
            time.sleep(0.05)
        return self.get(key)

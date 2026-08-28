import time
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch


def test_pg_ttl_allows_reinsert_after_expiry():
    """PG 过期行 ON CONFLICT 永久阻断 -> 修复后可重插"""
    from hero_quant.governance.dedup import DedupStore

    ttl = 1  # 1 second
    store = DedupStore(db_path="memory://dedup", ttl_seconds=ttl, dsn="postgresql://fake:5432/db")
    # simulate PG pool with in-memory rows dict: key -> updated_at
    rows = {}

    now = time.time()
    expired_key = "t:wf:step:tool:biz1"
    # pre-insert an expired row (updated_at far past)
    rows[expired_key] = {"tool": "tool", "updated_at": now - 100}

    class FakeCursor:
        def __init__(self, conn):
            self.conn = conn
            self.rowcount = 0
            self.description = None

        def execute(self, sql, params=None):
            sql_l = sql.lower() if isinstance(sql, str) else ""
            params = params or ()
            # DELETE expired: DELETE FROM dedup WHERE key=%s AND updated_at < now() - interval
            if "delete" in sql_l and "dedup" in sql_l:
                key = params[0] if params else None
                if key in rows:
                    # check expiry: updated_at < now - ttl
                    if time.time() - rows[key]["updated_at"] > store.ttl_seconds:
                        del rows[key]
                        self.rowcount = 1
                    else:
                        self.rowcount = 0
                else:
                    self.rowcount = 0
                return self
            # INSERT dedup
            if "insert into dedup" in sql_l or "insert into tool_call_dedup" in sql_l:
                key = params[0] if params else None
                if key in rows:
                    self.rowcount = 0
                else:
                    # insert new
                    rows[key] = {"tool": params[1] if len(params) > 1 else "tool", "updated_at": time.time()}
                    self.rowcount = 1
                return self
            # SELECT
            if "select" in sql_l:
                key = params[0] if params else None
                if key in rows:
                    # ttl filter: only return if not expired
                    if time.time() - rows[key]["updated_at"] > store.ttl_seconds:
                        self._row = None
                    else:
                        self._row = (key, rows[key]["tool"], "PENDING", None, rows[key]["updated_at"])
                        self.description = [("key",), ("tool",), ("status",), ("result",), ("updated_at",)]
                else:
                    self._row = None
                self.rowcount = 0
                return self
            return self

        def fetchone(self):
            return getattr(self, "_row", None)

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            cur = FakeCursor(self)
            return cur.execute(sql, params)

        def cursor(self):
            return FakeCursor(self)

        def commit(self):
            pass

    class FakePool:
        def connection(self):
            return FakeConn()

    store.pool = FakePool()
    # first re-insert attempt on expired key: current buggy code does NOT delete expired,
    # so ON CONFLICT DO NOTHING returns inserted=False
    # after fix, it deletes expired first, then inserted=True
    result = store.insert_pending(expired_key, "tool")
    assert result is True, "TTL expired row should allow reinsert (was permanently blocked by ON CONFLICT)"


def test_total_changes_bug_fixed():
    """测 UPDATE rowcount 而非 total_changes: UPDATE 0 行不应误判"""
    from hero_quant.governance.dedup import DedupStore

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "dedup.db"
        store = DedupStore(db_path=str(db_path), ttl_seconds=3600)

        # normal insert then mark_success should work
        key = "t:wf:step:tool:biz2"
        assert store.insert_pending(key, "tool") is True

        # Now test the bug: mark_success on a NEW key (no PENDING) should INSERT.
        # Buggy code uses con.total_changes which may be non-zero from prior ops,
        # causing it to skip INSERT fallback.
        # We mock sqlite3.connect to return a conn where UPDATE rowcount=0 but total_changes=1
        new_key = "t:wf:step:tool:biz_new"
        real_connect = sqlite3.connect

        class FakeCur:
            def __init__(self):
                self.rowcount = 0

            def execute(self, sql, params=None):
                sql_l = sql.lower()
                if "update" in sql_l:
                    self.rowcount = 0  # no row matched
                elif "insert" in sql_l:
                    # actually insert into real db via real connection
                    self.rowcount = 1
                    # delegate to real connection for side effect
                    self._real_cur.execute(sql, params)
                    self.rowcount = self._real_cur.rowcount
                return self

        # More direct: inspect source after fix should use rowcount not total_changes
        import pathlib
        src = pathlib.Path("src/hero_quant/governance/dedup.py").read_text(encoding="utf-8")
        # After fix, there should be no use of total_changes for winner check
        # Before fix, total_changes is used in mark_success/mark_failed
        assert "total_changes" not in src, "should use cur.rowcount not con.total_changes"

        # Functional check: mark_success on new key should create record via rowcount path
        store2 = DedupStore(db_path=str(db_path), ttl_seconds=3600)
        # use real DB to verify functional behavior after fix
        store2.mark_success(new_key, {"ok": 1})
        rec = store2.get(new_key)
        assert rec is not None and rec.get("status") == "SUCCESS", "mark_success should INSERT when UPDATE affects 0 rows"

        # Also verify sqlite atomicity: insert_pending uses BEGIN IMMEDIATE (inspect source)
        assert "BEGIN IMMEDIATE" in src, "insert_pending should use BEGIN IMMEDIATE for atomicity"
        assert "threading" in src, "_mem should use threading.Lock"
        assert "monotonic" in src, "wait_for should use monotonic"

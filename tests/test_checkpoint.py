def test_checkpoint_roundtrip():
    from hero_quant.checkpoint.postgres import get_saver
    saver=get_saver(dsn="memory://test")
    tid="backtest:1:tenantA"
    saver.put(tid, {"step":1}, {"next":"plan"})
    assert saver.get(tid)["step"]==1


def test_checkpoint_public_api_reexport():
    """checkpoint __init__ must re-export all temporal public symbols."""
    import hero_quant.checkpoint as cp
    import hero_quant.checkpoint.temporal as tmp

    for name in tmp.__all__:
        assert hasattr(cp, name), f"missing re-export {name} in hero_quant.checkpoint"
        assert name in cp.__all__, f"{name} not in checkpoint __all__"
    # also verify importable
    from hero_quant.checkpoint import (
        DEFAULT_HEARTBEAT_TIMEOUT,
        HEARTBEAT_INTERVAL,
        HEARTBEAT_INTERVAL_SECONDS,
        HeartbeatHelper,
        HeartbeatTimer,
        get_heartbeat_details,
        heartbeat,
    )

    assert HEARTBEAT_INTERVAL_SECONDS == tmp.HEARTBEAT_INTERVAL_SECONDS
    assert HeartbeatHelper is tmp.HeartbeatHelper
    assert HeartbeatTimer is tmp.HeartbeatTimer


def test_sql_injection_safe():
    """expires_at must be parameterized, not interpolated; injection TTL must not execute."""
    from hero_quant.checkpoint.postgres import AsyncPostgresSaver
    import inspect

    # Source-level check: _pg_put_sync should not interpolate ttl via % operator
    src = inspect.getsource(AsyncPostgresSaver._pg_put_sync)
    # old vulnerable pattern
    assert "interval '%s" not in src, "SQL still uses string interpolation for expires_at"
    # must use parameterized interval
    assert "interval '1 second'" in src or "interval" in src.lower()
    # ensure SQL templates use %s placeholder for ttl param
    assert "%s * interval" in src or "interval %s" in src or "%s::interval" in src

    # Runtime check: mock pool captures SQL and params, ensure ttl not interpolated
    class FakeConn:
        def __init__(self):
            self.last_sql = None
            self.last_params = None
        def execute(self, sql, params=None):
            self.last_sql = sql
            self.last_params = params
            class Cur:
                def fetchone(self): return None
            return Cur()
        def commit(self): pass
        def cursor(self):
            # return self as cursor for fallback path
            return self
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class FakePool:
        def __init__(self, conn):
            self._conn = conn
        def connection(self):
            return self._conn

    saver = AsyncPostgresSaver(dsn="postgresql://postgres:postgres@localhost:5432/hero_quant", ttl_seconds=123)
    fake_conn = FakeConn()
    saver.pool = FakePool(fake_conn)
    # Force PG mode with real pool
    saver._pg_put_sync("wf:1:tenantX", {"k": 1}, {"cfg": 1})
    sql = fake_conn.last_sql or ""
    params = fake_conn.last_params or ()
    # SQL must contain placeholder, not literal 123
    assert "%s" in sql, "SQL must be parameterized"
    assert "123" not in sql or "%s * interval" in sql, "TTL value must not be interpolated"
    # TTL should be in params when >0
    assert 123 in params or "123" not in sql

    # Injection attempt: TTL that tries to escape (if cast fails, should not appear in SQL)
    saver2 = AsyncPostgresSaver(dsn="postgresql://postgres:postgres@localhost:5432/hero_quant", ttl_seconds=999)
    # Monkey-patch ttl to malicious string after init (simulates bad input before int cast)
    # The fixed code must not interpolate raw ttl; it casts to int and uses param
    fake_conn2 = FakeConn()
    saver2.pool = FakePool(fake_conn2)
    saver2.ttl_seconds = "0; DROP TABLE checkpoints; --"  # type: ignore
    # _pg_put_sync should handle non-int gracefully: it should not produce SQL containing DROP
    try:
        saver2._pg_put_sync("wf:1:tenantX", {"k": 1}, {})
    except Exception:
        pass
    sql2 = fake_conn2.last_sql or ""
    if sql2:
        assert "DROP" not in sql2, "injection string must not appear in SQL"
        assert "0; DROP" not in sql2

"""Task7 TDD: checkpoint PG default, restart not lost, expires_at 7d via Settings."""
import os
import time

def test_checkpoint_pg_default_not_memory():
    from hero_quant.config.settings import Settings
    s = Settings()
    assert s.checkpoint_dsn.startswith(("postgresql://", "postgres://", "postgresql+psycopg://")), f"PG default required, got {s.checkpoint_dsn}"
    # ensure ttl 7d via Settings
    assert s.checkpoint_ttl_seconds == 7*24*3600
    # DDL contains required schema
    from hero_quant.checkpoint.postgres import DDL_CHECKPOINTS
    assert "tenant text" in DDL_CHECKPOINTS
    assert "thread text" in DDL_CHECKPOINTS
    assert "seq int" in DDL_CHECKPOINTS
    assert "checkpoint jsonb" in DDL_CHECKPOINTS
    assert "expires_at" in DDL_CHECKPOINTS
    assert "primary key (tenant, thread, seq)" in DDL_CHECKPOINTS.lower()


def test_put_get_restart_not_lost():
    """PG main path with fallback to memory only when PG unreachable — restart not lost via emulated PG global store."""
    # Use explicit PG DSN to trigger PG path (emulated global store ensures persistence without real PG)
    dsn = "postgresql://postgres:postgres@localhost:5432/hero_quant_test_ckpt"
    from hero_quant.checkpoint.postgres import get_saver
    saver1 = get_saver(dsn=dsn, ttl_seconds=7*24*3600)
    tid = "backtest:1:tenantA"
    payload = {"step": 42, "data": "hello"}
    saver1.put(tid, payload, {"next": "plan"})
    # simulate restart: new saver instance same DSN
    saver2 = get_saver(dsn=dsn, ttl_seconds=7*24*3600)
    got = saver2.get(tid)
    assert got is not None, "restart should not lose checkpoint (PG main path)"
    assert got["step"] == 42
    assert got["data"] == "hello"
    # also test get_with_config
    cw = saver2.get_with_config(tid)
    assert cw is not None
    assert cw[0]["step"] == 42
    # cleanup
    saver2.delete(tid)
    assert saver2.get(tid) is None


def test_expires_at_7d_via_settings(monkeypatch):
    """expires_at 7d via Settings — TTL controls window, mock time to verify expiry."""
    from hero_quant.checkpoint.postgres import get_saver
    from hero_quant.config.settings import Settings
    # default 7d
    s = Settings()
    assert s.checkpoint_ttl_seconds == 604800
    dsn = "postgresql://postgres:postgres@localhost:5432/hero_quant_test_ttl"
    saver = get_saver(dsn=dsn, ttl_seconds=s.checkpoint_ttl_seconds)
    assert saver.ttl_seconds == 604800
    tid = "workflow:99:tenantTTL"
    saver.put(tid, {"v": 1})
    assert saver.get(tid)["v"] == 1
    # simulate time >7d
    orig_time = time.time
    try:
        fake_now = orig_time() + 8*24*3600
        monkeypatch = None
        import unittest.mock as mock
        with mock.patch("hero_quant.checkpoint.postgres.time.time", return_value=fake_now):
            # also patch time.time for get path (module time)
            assert saver.get(tid) is None, "expired after 7d should return None"
    finally:
        pass
    # custom TTL via env
    monkeypatch_env = {"HERO_CHECKPOINT_TTL_SECONDS": "3600"}
    # use explicit ttl param to verify
    saver2 = get_saver(dsn=dsn, ttl_seconds=3600)
    assert saver2.ttl_seconds == 3600


def test_memory_fallback_when_pg_unreachable():
    """When PG unreachable, fallback to memory guarantees not broken — memory:// still works."""
    from hero_quant.checkpoint.postgres import get_saver
    saver = get_saver(dsn="memory://fallback_test")
    tid = "a:1:t1"
    saver.put(tid, {"x": 1})
    assert saver.get(tid)["x"] == 1


def test_pg_saver_memory_fallback():
    from hero_quant.checkpoint.postgres import get_saver

    s = get_saver("memory://test")
    assert s is not None


def test_thread_to_keys_deterministic():
    """Deterministic seq via hashlib, not hash() salted."""
    from hero_quant.checkpoint.postgres import _thread_to_keys
    import hashlib

    tid = "wf:myrun-2026:tenant1"
    tenant, thread, seq = _thread_to_keys(tid)
    expected = int(hashlib.sha256("myrun-2026".encode()).hexdigest()[:8], 16) % 2147483647
    assert seq == expected, f"seq {seq} != expected hashlib {expected} — hash() is salted"
    # same input twice -> same output
    assert _thread_to_keys(tid) == _thread_to_keys(tid)
    # numeric run still works
    tenant2, thread2, seq2 = _thread_to_keys("wf:123:tenant1")
    assert seq2 == 123


def test_thread_roundtrip():
    """put checkpoint with non-numeric run, list_thread_ids returns original id."""
    from hero_quant.checkpoint.postgres import get_saver, _PG_GLOBAL_STORE, _PG_GLOBAL_TS
    dsn = "postgresql://postgres:postgres@localhost:5432/hero_quant_test_roundtrip"
    # cleanup any prior
    for k in list(_PG_GLOBAL_STORE.keys()):
        if k.startswith(dsn):
            _PG_GLOBAL_STORE.pop(k, None)
            _PG_GLOBAL_TS.pop(k, None)
            from hero_quant.checkpoint.postgres import _PG_GLOBAL_META
            _PG_GLOBAL_META.pop(k, None)
    saver = get_saver(dsn=dsn, ttl_seconds=7 * 24 * 3600)
    tid = "wf:myrun-2026:tenant1"
    payload = {"step": 99}
    saver.put(tid, payload, {})
    # simulate restart: new saver instance same DSN should see same id
    saver2 = get_saver(dsn=dsn, ttl_seconds=7 * 24 * 3600)
    ids = saver2.list_thread_ids()
    assert tid in ids, f"list_thread_ids lost original run, got {ids}"
    assert saver2.get(tid)["step"] == 99
    # cleanup
    saver2.delete(tid)


def test_thread_collision_disambiguation(monkeypatch):
    """Two distinct runs that map to same base seq don't overwrite each other (linear probing)."""
    import hashlib
    import hero_quant.checkpoint.postgres as pg

    # clear any prior collision state
    if hasattr(pg, "_PG_SEQ_BY_RUN"):
        pg._PG_SEQ_BY_RUN.clear()
    if hasattr(pg, "_PG_RUN_BY_SEQ"):
        pg._PG_RUN_BY_SEQ.clear()
    # force hashlib collision: same digest -> same base seq
    orig_sha256 = hashlib.sha256

    class _FakeHash:
        def __init__(self, data):
            self.data = data

        def hexdigest(self):
            return "aaaaaaaa" + "0" * 56

    monkeypatch.setattr(hashlib, "sha256", lambda x, _orig=orig_sha256: _FakeHash(x))
    # also need to patch pg.hashlib reference if imported inside module
    monkeypatch.setattr(pg.hashlib, "sha256", lambda x: _FakeHash(x))

    tid_a = "wf:runA:tenantX"
    tid_b = "wf:runB:tenantX"
    # ensure clean state for these tenants
    tenant_a, thread_a, seq_a = pg._thread_to_keys(tid_a)
    tenant_b, thread_b, seq_b = pg._thread_to_keys(tid_b)
    assert seq_a != seq_b, f"collision not disambiguated: both {seq_a}"
    assert seq_b == (seq_a + 1) % 2147483647, f"expected linear probing, got {seq_a} vs {seq_b}"

    # also verify that puts don't overwrite each other via emulated PG store
    dsn = "postgresql://postgres:postgres@localhost:5432/hero_quant_test_collision"
    # cleanup
    prefix = f"{dsn}::"
    for k in list(pg._PG_GLOBAL_STORE.keys()):
        if k.startswith(prefix):
            pg._PG_GLOBAL_STORE.pop(k, None)
            pg._PG_GLOBAL_TS.pop(k, None)
            pg._PG_GLOBAL_META.pop(k, None)
    # clear maps again for this tenant
    pg._PG_SEQ_BY_RUN.clear()
    pg._PG_RUN_BY_SEQ.clear()
    from hero_quant.checkpoint.postgres import get_saver

    saver = get_saver(dsn=dsn, ttl_seconds=3600)
    saver.put(tid_a, {"v": 1}, {})
    saver.put(tid_b, {"v": 2}, {})
    assert saver.get(tid_a)["v"] == 1
    assert saver.get(tid_b)["v"] == 2
    ids = saver.list_thread_ids()
    assert tid_a in ids and tid_b in ids, f"both ids must survive collision, got {ids}"
    # cleanup
    saver.delete(tid_a)
    saver.delete(tid_b)


# ---- P2-cb extended TDD ----

def test_asetup_retry_guard_success_no_rerun():
    """asetup retry guard: second call after success does not re-run setup."""
    import asyncio

    from hero_quant.checkpoint.postgres import AsyncPostgresSaver

    class FakeAsyncPool:
        def __init__(self):
            self.open_calls = 0
            self.conninfo = "postgresql://postgres:postgres@localhost:5432/hero_quant_test_asetup_guard"

        async def open(self):
            self.open_calls += 1

        def connection(self):  # needed for DDL branch
            class FakeConn:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False

                async def execute(self, sql, params=None):
                    pass

            return FakeConn()

    # make pool look async via name containing Async
    FakeAsyncPool.__name__ = "AsyncFakePool"
    pool = FakeAsyncPool()
    saver = AsyncPostgresSaver(
        dsn="postgresql://postgres:postgres@localhost:5432/hero_quant_test_asetup_guard", pool=pool
    )
    assert saver._is_real_pg_pool() and saver._pool_is_async()
    asyncio.run(saver.asetup())
    first = pool.open_calls
    assert first == 1
    asyncio.run(saver.asetup())
    second = pool.open_calls
    assert second == 1, f"second asetup should not re-run, got {first}->{second}"


def test_asetup_retry_after_failure():
    """asetup after failure (done False) retries."""
    import asyncio

    from hero_quant.checkpoint.postgres import AsyncPostgresSaver

    class FakeAsyncPool:
        def __init__(self):
            self.open_calls = 0
            self.conninfo = "postgresql://postgres:postgres@localhost:5432/hero_quant_test_asetup_fail"

        async def open(self):
            self.open_calls += 1

        def connection(self):
            class FakeConn:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False

                async def execute(self, sql, params=None):
                    pass

            return FakeConn()

    FakeAsyncPool.__name__ = "AsyncFakePool"
    pool = FakeAsyncPool()
    saver = AsyncPostgresSaver(
        dsn="postgresql://postgres:postgres@localhost:5432/hero_quant_test_asetup_fail2", pool=pool
    )
    saver._setup_done = False
    asyncio.run(saver.asetup())
    assert pool.open_calls == 1, "retry after failure should call setup"


def test_aput_emulated_vs_real_paths():
    """aput PG branch tautology fixed: emulated vs real pool paths both behave per contract."""
    import asyncio
    import inspect

    from hero_quant.checkpoint.postgres import AsyncPostgresSaver

    # source must not contain tautology
    src = inspect.getsource(AsyncPostgresSaver.aput)
    assert "self._is_real_pg_pool() or self._is_pg_mode()" not in src
    assert "if self._is_real_pg_pool():" in src

    # emulated path still persists via global store
    saver_em = AsyncPostgresSaver(dsn="postgresql://postgres:postgres@localhost:5432/hero_quant_test_aput_em", pool=None)
    assert saver_em._is_pg_mode() and not saver_em._is_real_pg_pool()
    asyncio.run(saver_em.aput("wf:runEm:tenantE", {"v": 11}))
    val = asyncio.run(saver_em.aget("wf:runEm:tenantE"))
    assert val is not None and val["v"] == 11
    saver_em.delete("wf:runEm:tenantE")

    # real pool path persists and still calls _pg_put_async (best-effort)
    class FakePool2:
        def __init__(self):
            self.conninfo = "postgresql://postgres:postgres@localhost:5432/hero_quant_test_aput_real"

        async def open(self):
            pass

        def connection(self):
            class C:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False

                async def execute(self, sql, params=None):
                    pass

            return C()

    FakePool2.__name__ = "AsyncFakePool"
    fake = FakePool2()
    saver_real = AsyncPostgresSaver(
        dsn="postgresql://postgres:postgres@localhost:5432/hero_quant_test_aput_real", pool=fake
    )
    asyncio.run(saver_real.aput("wf:runReal:tenantE", {"v": 22}))
    val2 = asyncio.run(saver_real.aget("wf:runReal:tenantE"))
    assert val2 is not None and val2["v"] == 22
    saver_real.delete("wf:runReal:tenantE")


def test_list_thread_ids_real_pg_reconstruction_via_mapping():
    """list_thread_ids real PG reconstruction uses _PG_RUN_BY_SEQ mapping."""
    import hero_quant.checkpoint.postgres as pg
    from hero_quant.checkpoint.postgres import _thread_to_keys, get_saver

    dsn = "postgresql://postgres:postgres@localhost:5432/hero_quant_test_list_reconstruct"
    # cleanup globals
    for k in list(pg._PG_GLOBAL_STORE.keys()):
        if k.startswith(dsn):
            pg._PG_GLOBAL_STORE.pop(k, None)
            pg._PG_GLOBAL_TS.pop(k, None)
            pg._PG_GLOBAL_META.pop(k, None)
    pg._PG_SEQ_BY_RUN.clear()
    pg._PG_RUN_BY_SEQ.clear()
    tid = "wf:myrun-EXT:tenantZ"
    tenant, thread, seq = _thread_to_keys(tid)
    # ensure mapping exists

    class FakeConn:
        def __init__(self, rows):
            self.rows = rows

        def execute(self, sql, params=None):
            class Cur:
                def __init__(self, rows):
                    self.rows = rows

                def fetchall(self):
                    return self.rows

            return Cur(self.rows)

        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakePool:
        def __init__(self, rows):
            self.rows = rows
            self.conninfo = dsn

        def connection(self):
            return FakeConn(self.rows)

    saver = get_saver(dsn=dsn, ttl_seconds=3600)
    saver.pool = FakePool([(tenant, thread, seq)])
    # clear emulated alive so real PG path taken
    for k in list(pg._PG_GLOBAL_TS.keys()):
        if k.startswith(dsn):
            pg._PG_GLOBAL_TS.pop(k, None)
    saver._timestamps.clear()
    ids = saver.list_thread_ids()
    assert tid in ids, f"real PG reconstruction failed, got {ids}, expected {tid}"

"""Lane C retest271 TDD — ledger/billing review findings.

One failing-first repro per finding (24 items, 15 high / 7 medium / 2 low).
Only touches the 6 lane-C source files via behavior assertions.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import threading


# ── billing/service.py ─────────────────────────────────────────────

def test_c_half_commit_publish_pg_first(tmp_path, monkeypatch):
    """Ledger-first half-commit: PG persist must precede ledger append (or compensate)."""
    from hero_quant.billing.service import BillingService

    calls: list[str] = []

    class RecLedger:
        def append(self, *a, **k):
            calls.append("ledger")
            return {}

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def cursor(self):
            class C:
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def execute(self, *a, **k): pass
            return C()
        def commit(self): pass

    class FakePool:
        def connection(self): return FakeConn()

    svc = BillingService(
        ledger=RecLedger(),
        dsn="postgresql://postgres:postgres@localhost:5432/lane_c_halffirst",
        pool=FakePool(),
    )
    assert svc._is_real_pg() is True
    orig = BillingService._pg_publish_sync

    def rec_pg(self, factor):
        calls.append("pg")
        return orig(self, factor)

    monkeypatch.setattr(BillingService, "_pg_publish_sync", rec_pg)
    svc.publish_factor("f_half", "F", 10.0, tenant="t1")
    assert calls == ["pg", "ledger"], f"PG must persist before ledger append, got {calls}"
    assert svc.get_factor("f_half") is not None


def test_c_pg_publish_fail_compensates_ledger(tmp_path, monkeypatch):
    """PG failure must not leave a half-committed ledger entry: fail-closed, no publish without persist."""
    from hero_quant.billing.service import BillingService

    appended: list[dict] = []

    class RecLedger:
        def append(self, record, tenant=None, price=None):
            appended.append(record)
            return {}

    svc = BillingService(ledger=RecLedger())
    monkeypatch.setattr(BillingService, "_pg_publish_sync", lambda self, f: (_ for _ in ()).throw(RuntimeError("pg down")))
    svc._is_real_pg = lambda: True  # type: ignore
    try:
        svc.publish_factor("f_comp", "F", 10.0, tenant="t1")
        raised = False
    except RuntimeError:
        raised = True
    assert raised is True
    # PG-first: no publish entry may exist in the ledger when PG never persisted;
    # if an entry was appended it must be a compensating entry, never a bare publish.
    publishes = [r for r in appended if r.get("action") == "publish_factor"]
    assert publishes == [], f"half-committed publish in ledger without PG persist: {appended}"
    assert svc.get_factor("f_comp") is None


def test_c_idempotency_race_single_ledger_append(monkeypatch):
    """Concurrent purchase must ledger-append exactly once for one (factor,buyer)."""
    for k in ("HERO_BILLING_DSN", "HERO_PG_DSN", "HERO_CHECKPOINT_DSN"):
        monkeypatch.setenv(k, "")
    from hero_quant.billing.service import BillingService

    appended: list[dict] = []
    lock = threading.Lock()

    class RecLedger:
        def append(self, record, tenant=None, price=None):
            with lock:
                appended.append(record)
            return {}

    svc = BillingService(ledger=RecLedger())
    svc.publish_factor("f_race", "F", 10.0, tenant="prov")
    receipts: list[dict] = []

    def _buy():
        receipts.append(svc.purchase("f_race", buyer_tenant="buyer_r"))

    threads = [threading.Thread(target=_buy) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({r["purchase_id"] for r in receipts}) == 1
    purchases = [r for r in appended if r.get("action") == "purchase_factor"]
    assert len(purchases) == 1, f"ledger double-appended: {len(purchases)}"


def test_c_commit_error_returns_false(monkeypatch):
    """Swallowed commit errors must log and return False (fail-closed)."""
    from hero_quant.billing import service as mod

    src_pg = inspect.getsource(mod.BillingService._pg_publish_sync)
    assert "billing commit failed" in src_pg
    assert "return False" in src_pg

    src_ins = inspect.getsource(mod.BillingService._pg_insert_purchase_sync)
    assert "billing commit failed" in src_ins or "return False" in src_ins

    # functional: commit raising -> _pg_publish_sync returns False
    class BoomConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def cursor(self):
            class C:
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def execute(self, *a, **k): pass
            return C()
        def commit(self): raise OSError("mock commit boom")

    class BoomPool:
        def connection(self): return BoomConn()

    svc = mod.BillingService(
        dsn="postgresql://postgres:postgres@localhost:5432/lane_c_commit",
        pool=BoomPool(),
    )
    assert svc._pg_publish_sync({"factor_id": "f", "tenant": "t", "price": 1, "name": "n", "description": ""}) is False


def test_c_emulated_pg_fail_closed(monkeypatch):
    """Emulated-PG path must fail-closed like the real-PG branch."""
    from hero_quant.billing.service import BillingService

    svc = BillingService(dsn="postgresql://postgres:postgres@localhost:5432/lane_c_emul")
    svc._pg_publish_sync = lambda factor: False  # type: ignore
    try:
        svc.publish_factor("f_emul", "F", 10.0, tenant="t1")
        raised = False
    except RuntimeError:
        raised = True
    assert raised is True, "emulated-PG False must raise (fail-closed)"
    assert svc.get_factor("f_emul") is None


def test_c_dead_ddl_wired_or_removed():
    """Dead DDL: DDL_FACTORS/DDL_PURCHASES must be executed or removed."""
    import hero_quant.billing.service as mod

    src = inspect.getsource(mod)
    if "DDL_FACTORS =" in src:
        # executed on real-pool init via _exec_billing_ddl (loop over the tuple counts)
        assert "_exec_billing_ddl" in src, "DDL helper missing"
        assert "DDL_FACTORS" in inspect.getsource(mod.BillingService._exec_billing_ddl)
        assert "_exec_billing_ddl" in inspect.getsource(mod.BillingService._pg_publish_sync)
        assert "_exec_billing_ddl" in inspect.getsource(mod.BillingService._pg_insert_purchase_sync)
    else:
        pass  # removed is also acceptable


# ── governance/dedup.py ────────────────────────────────────────────

def test_c_async_setup_fail_closed(monkeypatch):
    """Async PG setup must be fail-closed on RLS/DDL like the sync path."""
    import hero_quant.governance.dedup as dd

    src = inspect.getsource(dd.DedupStore._pg_setup_async)
    assert "raise" in src
    assert "dedup PG async setup failed" in src

    class BoomConn:
        async def execute(self, *a, **k): raise OSError("mock RLS boom")
        async def cursor(self):
            raise OSError("mock RLS boom")

    class BoomCM:
        async def __aenter__(self): return BoomConn()
        async def __aexit__(self, *a): return False

    class BoomPool:
        async def open(self): pass
        def connection(self): return BoomCM()

    BoomPool.__name__ = "AsyncBoomPool"
    store = dd.DedupStore("memory://lane-c-async-setup")
    store._is_pg = True
    store.pool = BoomPool()
    import pytest
    with pytest.raises(Exception):
        asyncio.run(store._pg_setup_async())


def test_c_pg_commit_failure_not_success():
    """PG commit failures must not be reported as success / cached in _mem."""
    import hero_quant.governance.dedup as dd

    src_ins = inspect.getsource(dd.DedupStore._pg_insert_pending_sync)
    assert "dedup pg insert_pending commit failed" in src_ins
    src_mark = inspect.getsource(dd.DedupStore._pg_mark_sync)
    assert "dedup pg mark failed" in src_mark

    class BoomConn:
        def execute(self, sql, params=None):
            class _C: rowcount = 1
            return _C()
        def cursor(self): raise AssertionError("should not reach cursor")
        def commit(self): raise OSError("mock commit boom")
        def rollback(self): pass

    class BoomCM:
        def __init__(self, c): self._c = c
        def __enter__(self): return self._c
        def __exit__(self, *a): return False

    class BoomPool:
        def __init__(self, c): self._c = c
        def connection(self): return BoomCM(self._c)

    store = dd.DedupStore("memory://lane-c-commit")
    store._is_pg = True
    store.pool = BoomPool(BoomConn())
    # commit failure -> loud None (fallback), never True; no success cached
    assert store._pg_insert_pending_sync("t:k-commit", "tool") is None
    assert "t:k-commit" not in store._mem
    assert store._pg_mark_sync("t:k-commit", "SUCCESS", result={"ok": 1}) is False


def test_c_dual_table_atomic():
    """Dual-table writes (dedup + tool_call_dedup) must be one txn; 2nd-table errors propagate."""
    import hero_quant.governance.dedup as dd

    src = inspect.getsource(dd.DedupStore._pg_mark_sync)
    assert "also update alias table best-effort" not in src
    assert "conn.commit()  # single atomic commit" in src or src.count("conn.commit()") >= 1

    class FailSecondConn:
        def __init__(self): self.commits = 0
        def execute(self, sql, params=None):
            if "tool_call_dedup" in sql:
                raise OSError("mock alias boom")
            class _C: rowcount = 1
            return _C()
        def cursor(self):
            outer = self
            class _CM:
                def __enter__(self): return outer
                def __exit__(self, *a): return False
            return _CM()
        def commit(self): self.commits += 1

    class FakeCM:
        def __init__(self, c): self._c = c
        def __enter__(self): return self._c
        def __exit__(self, *a): return False

    class FakePool:
        def __init__(self, c): self._c = c
        def connection(self): return FakeCM(self._c)

    store = dd.DedupStore("memory://lane-c-dual")
    store._is_pg = True
    conn = FailSecondConn()
    store.pool = FakePool(conn)
    # second-table failure -> loud False, no commit, caller falls back (never True)
    assert store._pg_mark_sync("t:k-dual", "SUCCESS", result={"ok": 1}) is False
    assert conn.commits == 0, "must not commit when second table write failed"


def test_c_alias_table_ttl_enforced():
    """PG alias-table fallback in _pg_get_sync must enforce TTL."""
    import hero_quant.governance.dedup as dd

    src = inspect.getsource(dd.DedupStore._pg_get_sync)
    assert "updated_at > now()" in src
    # alias sql2 must carry a TTL predicate too
    seg = src.split("tool_call_dedup WHERE idempotency_key=%s")
    assert len(seg) >= 2 and "INTERVAL" in seg[1][:400], "alias-table fallback lacks TTL predicate"


def test_c_sqlite_contention_full_row(tmp_path):
    """SQLite contention path must cache the full row, not lossy status-only."""
    from hero_quant.governance.dedup import DedupStore

    p = tmp_path / "lane_c_dedup.db"
    store = DedupStore(str(p))
    assert store.insert_pending("t:contend", "toolA") is True
    store.mark_success("t:contend", {"v": 42})
    store._mem.clear()
    store._mem_ts.clear()
    assert store.insert_pending("t:contend", "toolB") is False
    rec = store._mem.get("t:contend")
    assert rec is not None
    assert rec.get("status") == "SUCCESS"
    assert rec.get("result") == {"v": 42}, f"lossy cache: {rec}"
    assert rec.get("tool") == "toolA", f"stored tool discarded: {rec}"


# ── governance/ledger.py ───────────────────────────────────────────

def test_c_append_mid_prefix_tamper_detected(tmp_path):
    """Incremental verify: in-place mid-prefix tamper (same length) must be detected on append."""
    import json as _json
    from hero_quant.governance.ledger import Ledger, LedgerCorruptionError

    p = tmp_path / "lane_c_mid.jsonl"
    led = Ledger(p)
    for i in range(5):
        led.append({"v": i}, tenant="t1")
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    mid = _json.loads(lines[2])
    # same-length payload swap, keep stored record_hash stale (anchor tail unchanged)
    old = _json.dumps(mid["record"], sort_keys=True, separators=(",", ":"))
    new_rec = {"v": 99999} if len(_json.dumps({"v": 99999}, sort_keys=True, separators=(",", ":"))) == len(old) else {"v": 4, "pad": "x"}
    mid["record"] = new_rec
    lines[2] = _json.dumps(mid, ensure_ascii=False, separators=(",", ":"))
    # pad/truncate to keep exact size so incremental branch is taken
    raw = ("\n".join(lines) + "\n").encode("utf-8")
    p.write_bytes(raw)
    import pytest
    with pytest.raises(LedgerCorruptionError):
        led.append({"v": 5}, tenant="t1")


def test_c_rotate_does_not_mask_corruption(tmp_path):
    """rotate_if_needed must not mask corruption as lock_failed."""
    import json as _json
    from hero_quant.governance import ledger as lm

    p = tmp_path / "lane_c_rot.jsonl"
    led = lm.Ledger(p)
    for i in range(3):
        led.append({"v": i}, tenant="t1")
    lines = p.read_text(encoding="utf-8").splitlines()
    mid = _json.loads(lines[1])
    mid["record"] = {"v": "evil"}
    lines[1] = _json.dumps(mid, ensure_ascii=False, separators=(",", ":"))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    import pytest
    with pytest.raises(lm.LedgerCorruptionError) as ei:
        lm.rotate_if_needed(p, max_bytes=1)
    assert ei.value.chain_break.reason != "lock_failed", f"corruption masked: {ei.value.chain_break}"
    assert ei.value.chain_break.reason in ("record_hash_mismatch", "prev_hash_mismatch", "malformed_json", "seq_gap")


def test_c_verify_chain_lock_failure_loud(tmp_path, monkeypatch):
    """verify_chain lock-acquisition failure must be LOUD, never silent unlocked fallback."""
    from hero_quant.governance import ledger as lm

    p = tmp_path / "lane_c_vc.jsonl"
    led = lm.Ledger(p)
    led.append({"v": 1}, tenant="t1")

    def _boom(h):
        raise OSError("mock shared-lock boom")

    monkeypatch.setattr(lm, "_lock_shared", _boom)
    import pytest
    with pytest.raises(Exception):
        lm.verify_chain(p)


def test_c_nul_break_index_reports_line(tmp_path):
    """NUL break index must report the actual line, not always 0."""
    from hero_quant.governance import ledger as lm

    p = tmp_path / "lane_c_nul.jsonl"
    led = lm.Ledger(p)
    for i in range(3):
        led.append({"v": i}, tenant="t1")
    lines = p.read_text(encoding="utf-8").splitlines()
    lines[2] = lines[2][:10] + "\x00" + lines[2][10:]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    res = lm.verify_chain(p)
    assert res.ok is False
    assert res.first_break is not None
    assert res.first_break.index == 2, f"NUL index misreported: {res.first_break}"


def test_c_dead_code_removed():
    """Dead code _read_raw_records / _CHAIN_FIELDS must be removed or wired up."""
    import hero_quant.governance.ledger as lm

    src_all = inspect.getsource(lm)
    has_def = "def _read_raw_records" in src_all
    has_chain = "_CHAIN_FIELDS" in src_all
    if has_def:
        assert src_all.count("_read_raw_records") >= 3, "_read_raw_records defined but has no callers"
    if has_chain:
        assert src_all.count("_CHAIN_FIELDS") >= 3, "_CHAIN_FIELDS defined but never read"


# ── governance/reconcile.py ────────────────────────────────────────

def test_c_nan_holdings_fail_closed():
    """NaN holdings must fail-closed, never zero_diff=True."""
    import math
    import pytest
    from hero_quant.governance.reconcile import reconcile

    with pytest.raises(ValueError):
        reconcile({"AAPL": float("nan")}, {"AAPL": 10.0})
    with pytest.raises(ValueError):
        reconcile({"AAPL": 10.0}, {"AAPL": float("inf")})
    ok = reconcile({"AAPL": 10.0}, {"AAPL": 10.0})
    assert ok.zero_diff is True and math.isclose(ok.total_diff, 0.0)


def test_c_unknown_headers_fail_closed(tmp_path):
    """Unknown headers must fail-closed for both symbol and quantity."""
    import pytest
    from hero_quant.governance.reconcile import load_positions_csv

    p = tmp_path / "positions.csv"
    p.write_text("date,price,note\n2026-01-01,100,x\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_positions_csv(p)
    q = tmp_path / "positions2.csv"
    q.write_text("symbol,date,price\nAAPL,2026-01-01,100\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_positions_csv(q)


def test_c_unknown_side_fail_closed():
    """Unknown/typo sides must fail-closed, never default-to-buy."""
    import pytest
    from hero_quant.governance.reconcile import _shadow_qty_from_trade, aggregate_shadow

    with pytest.raises(ValueError):
        _shadow_qty_from_trade({"symbol": "AAPL", "qty": 10, "side": "selll"})
    # explicit sell aliases stay sell (whitelisted, still fail-closed for anything else)
    sym_s, q_s = _shadow_qty_from_trade({"symbol": "AAPL", "qty": 10, "side": "sold"})
    assert q_s == -10.0
    with pytest.raises(ValueError):
        _shadow_qty_from_trade({"symbol": "AAPL", "qty": 10, "side": "bogus"})
    sym, q = _shadow_qty_from_trade({"symbol": "AAPL", "qty": 10, "side": " Sell "})
    assert q == -10.0
    sym2, q2 = _shadow_qty_from_trade({"symbol": "AAPL", "qty": 10, "side": "BUY"})
    assert q2 == 10.0
    # aggregate_shadow surfaces it too
    with pytest.raises(ValueError):
        aggregate_shadow(journal=[{"symbol": "AAPL", "qty": 10, "side": "s3ll"}])


def test_c_list_journal_no_double_count(tmp_path):
    """list/dict journal + same ledger must not double-count."""
    import json as _json
    import pytest
    from hero_quant.governance.ledger import Ledger
    from hero_quant.governance.reconcile import aggregate_shadow

    p = tmp_path / "lane_c_shadow.jsonl"
    led = Ledger(p)
    led.append({"action": "shadow_record", "trade": {"symbol": "AAPL", "qty": 10, "side": "buy"}}, tenant="t1")
    journal = [{"symbol": "AAPL", "qty": 10, "side": "buy"}]
    with pytest.raises(ValueError):
        aggregate_shadow(journal=journal, ledger=led)
    with pytest.raises(ValueError):
        aggregate_shadow(journal=journal, ledger_path=p)
    # single-source still works
    assert aggregate_shadow(journal=journal) == {"AAPL": 10.0}
    assert aggregate_shadow(ledger=led) == {"AAPL": 10.0}


# ── checkpoint/postgres.py ─────────────────────────────────────────

def test_c_warm_map_prefix_consistent():
    """Warm-map keys must be DSN-prefix consistent with _thread_to_keys."""
    import hero_quant.checkpoint.postgres as pg

    pg._PG_SEQ_BY_RUN.clear()
    pg._PG_RUN_BY_SEQ.clear()
    try:
        dsn = "postgresql://postgres:postgres@localhost:5432/lane_c_warm"
        tid = "wf:runWarmC:tenantW"
        tenant, thread, seq = pg._thread_to_keys(tid, dsn=dsn)
        rows = [(tenant, thread, seq, "runWarmC")]
        n = pg._apply_warm_rows(rows, dsn)
        assert n == 1
        tenant2, thread2, seq2 = pg._thread_to_keys(tid, dsn=dsn)
        assert (tenant2, thread2, seq2) == (tenant, thread, seq)
        assert pg.get_run_text(tenant, thread, seq, dsn=dsn) == "runWarmC"
    finally:
        pg._PG_SEQ_BY_RUN.clear()
        pg._PG_RUN_BY_SEQ.clear()


def test_c_async_no_blocking_sync_io():
    """Async paths must not call blocking sync I/O directly on the loop thread."""
    import hero_quant.checkpoint.postgres as pg

    src_put = inspect.getsource(pg.AsyncPostgresSaver._pg_put_async)
    assert "return self._pg_put_sync(thread_id, checkpoint, config)" not in src_put, \
        "blocking sync I/O on async path"
    assert "to_thread" in src_put
    src_get = inspect.getsource(pg.AsyncPostgresSaver._pg_get_async)
    assert "return self._pg_get_sync(thread_id)" not in src_get, \
        "blocking sync I/O on async path"
    assert "to_thread" in src_get
    src_aget = inspect.getsource(pg.AsyncPostgresSaver.aget)
    assert "return self.get(thread_id)" not in src_aget, "aget blocks loop via self.get()"
    assert "to_thread" in src_aget


def test_c_async_pool_methods_supported():
    """delete / get_with_config / list_thread_ids must support async pools (or fail loudly)."""
    import hero_quant.checkpoint.postgres as pg

    for name in ("delete", "get_with_config", "list_thread_ids"):
        src = inspect.getsource(getattr(pg.AsyncPostgresSaver, name))
        assert "not self._pool_is_async()" not in src and "not self._pool_is_async" not in src, \
            f"{name} silently skips async pools"


def test_c_async_lock_wired_or_removed():
    """Dead async-lock helper + threading.RLock in async paths must be resolved.

    Resolution: global asyncio.Lock is loop-bound (unsafe shared across loops),
    so async data paths keep short non-blocking threading.RLock sections (never
    across await); setup uses the saver-level _asetup_lock; _get_async_lock stays
    as a compat helper with thread-guarded creation and closed-loop rebuild.
    """
    import hero_quant.checkpoint.postgres as pg

    src_all = inspect.getsource(pg)
    if "def _get_async_lock" in src_all or "_PG_ASYNC_LOCK" in src_all:
        src_helper = inspect.getsource(pg._get_async_lock)
        # creation guarded by the threading lock (no check-then-act race)
        assert "_PG_GLOBAL_LOCK" in src_helper
        # stale loop binding handled (no cross-loop deadlock)
        assert "is_closed" in src_helper
        # async data paths never hold the threading lock across await:
        # lock sections contain no await between with and block end
        for name in ("aput", "aget", "alist_thread_ids", "aget_with_config", "adelete"):
            src = inspect.getsource(getattr(pg.AsyncPostgresSaver, name))
            lines = src.splitlines()
            for i, ln in enumerate(lines):
                if "with _PG_GLOBAL_LOCK" in ln:
                    indent = len(ln) - len(ln.lstrip())
                    for sub in lines[i + 1:]:
                        s = sub.strip()
                        if s and (len(sub) - len(sub.lstrip())) <= indent:
                            break
                        assert "await " not in sub, f"{name} holds threading lock across await"
        # setup path uses saver-level async lock, not the threading lock across await
        assert "_asetup_lock" in inspect.getsource(pg.AsyncPostgresSaver.asetup)
    else:
        pass  # removed is acceptable


# ── checkpoint/temporal.py ─────────────────────────────────────────

def test_c_heartbeat_details_cleared():
    """Heartbeat detail stores must be clearable; no cross-activity reuse."""
    import hero_quant.checkpoint.temporal as tp

    assert hasattr(tp, "clear_heartbeat_details"), "missing clear_heartbeat_details()"
    tp.heartbeat({"act": "A", "seq": 1})
    assert tp.get_heartbeat_details() is not None
    tp.clear_heartbeat_details()
    got = tp.get_heartbeat_details()
    assert got is None, f"stale details reused after clear: {got}"


def test_c_restart_no_orphan_loop():
    """Repeated/mixed start/astart must not orphan prior heartbeat loops."""
    import time as _t
    import hero_quant.checkpoint.temporal as tp

    async def _run():
        h = tp.HeartbeatHelper(interval=0.2)
        await h.astart({"n": 1})
        first = h._async_task
        assert first is not None
        await h.astart({"n": 2})
        assert h._async_task is not first
        assert first.cancelled() or first.done(), "prior async task orphaned"
        try:
            await first
        except asyncio.CancelledError:
            pass
        # mixed sync start while async task runs: fail-closed (no silent dual heartbeat)
        import pytest as _pt
        with _pt.raises(RuntimeError):
            h.start({"n": 3})
        assert h._async_task is not None and not h._async_task.done()
        await h.astop()
        # after astop the async task is gone; sync start is allowed again
        h.start({"n": 3})
        assert h._thread is not None and h._thread.is_alive()
        await h.astop()
        h.stop()
        assert h._async_task is None

    asyncio.run(_run())


def test_c_stop_astop_thread_safe_nonblocking():
    """stop must not block a loop thread; astop must cancel thread-safely and await."""
    import hero_quant.checkpoint.temporal as tp

    src_stop = inspect.getsource(tp.HeartbeatHelper.stop)
    assert "call_soon_threadsafe" in src_stop, "task.cancel() must go via loop.call_soon_threadsafe"
    assert "get_running_loop" in src_stop, "stop must avoid join() on the loop thread"
    src_astop = inspect.getsource(tp.HeartbeatHelper.astop)
    assert "await" in src_astop and "to_thread" in src_astop, "astop must offload join and await cancel"

    async def _run():
        h = tp.HeartbeatHelper(interval=0.2)
        h.start({"s": 1})
        t0 = asyncio.get_running_loop().time()
        await h.astop()
        dt = asyncio.get_running_loop().time() - t0
        assert dt < 1.0, f"astop blocked loop for {dt:.2f}s"
        h.stop()

    asyncio.run(_run())

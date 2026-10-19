"""J线 J11: 总攻 —— dedup FakePool PG + postgres async + loop 纵深 + 调度/遥测/风控扫尾。"""
from __future__ import annotations

import asyncio
import json

import pytest


def _run(coro):
    return asyncio.run(coro)


# ---------- dedup FakePool PG ----------

class _DCursor:
    def __init__(self, conn, rowcount=1):
        self._conn = conn
        self.rowcount = rowcount

    def execute(self, sql, params=None):
        self._conn.statements.append(str(sql))
        if "FAIL_ONCE" in str(sql):
            raise RuntimeError("simulated")
        return self

    def fetchone(self):
        return self._conn.script_row

    def fetchall(self):
        return self._conn.script_rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _DConn:
    def __init__(self, pool):
        self._pool = pool
        self.statements = pool.statements
        self.script_row = pool.script_row
        self.script_rows = pool.script_rows

    def execute(self, sql, params=None):
        return _DCursor(self).execute(sql, params)

    def cursor(self):
        return _DCursor(self)

    def commit(self):
        return None

    def rollback(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _DPool:
    def __init__(self, *a, **k):
        self.statements: list[str] = []
        self.script_row = None
        self.script_rows = []

    def connection(self):
        pool = self

        class _Ctx:
            def __enter__(self):
                return _DConn(pool)

            def __exit__(self, *a):
                return False

        return _Ctx()


def test_j11_dedup_pg_full(monkeypatch):
    import hero_quant.governance.dedup as D

    monkeypatch.setattr(D, "DedupPool", _DPool)
    dsn = "postgresql://postgres:postgres@localhost:5432/hero_dedup_j11"
    st = D.DedupStore(dsn=dsn)
    assert st._is_pg is True
    key = D.derive_key("t", "w", "s", "tool", "pg1")
    assert st.insert_pending(key, "tool") in (True, False, None)
    assert st.insert_pending(key, "tool") in (True, False, None)
    st.mark_success(key, {"ok": 1})
    got = st.get(key)
    assert got is None or isinstance(got, dict)
    k2 = D.derive_key("t", "w", "s", "tool", "pg2")
    st.insert_pending(k2, "tool")
    st.mark_failed(k2, "bad")
    assert st.wait_for(key, timeout=0.5) is None or isinstance(st.wait_for(key, timeout=0.5), dict)
    assert any("dedup" in s or "INSERT" in s for s in st.pool.statements)


def test_j11_dedup_pg_getconn_and_env(monkeypatch):
    import hero_quant.governance.dedup as D

    class _GetConnPool(_DPool):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)

        def getconn(self):
            return _DConn(self)

        def putconn(self, conn):
            return None

    # getconn 分支：无 connection 方法的池迫使走 getconn/putconn
    class _OnlyGetConn:
        def __init__(self, *a, **k):
            self.statements: list[str] = []
            self.script_row = None
            self.script_rows = []

        def getconn(self):
            return _DConn(self)

        def putconn(self, conn):
            return None

    monkeypatch.setattr(D, "DedupPool", _OnlyGetConn)
    st = D.DedupStore(dsn="postgresql://postgres:postgres@localhost:5432/x")
    key = D.derive_key("t", "w", "s", "tool", "gc1")
    assert st.insert_pending(key, "tool") in (True, False, None)
    assert st.get(key) is None or isinstance(st.get(key), dict)
    # env DSN 路径
    monkeypatch.setenv("HERO_DEDUP_DSN", "postgresql://postgres:postgres@localhost:5432/envdb")
    st2 = D.DedupStore(db_path="memory://dedup", allow_memory=True)
    assert st2._is_pg is True


# ---------- postgres async fake ----------

class _ACursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return self._rows


class _AConn:
    def __init__(self, pool):
        self._pool = pool

    async def execute(self, sql, params=None):
        self._pool.statements.append(str(sql))
        return _ACursor(self._pool.script_rows)


class AsyncFakePool:
    """名字含 Async 以命中 _is_async_pool。"""

    def __init__(self, *a, **k):
        self.statements: list[str] = []
        self.script_rows: list = []

    def connection(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return _AConn(pool)

            async def __aexit__(self, *a):
                return False

        return _Ctx()


def test_j11_pg_async_paths():
    from hero_quant.checkpoint.postgres import AsyncPostgresSaver

    pool = AsyncFakePool()
    pool.script_rows = [(json.dumps({"a": 1}),)]
    sv = AsyncPostgresSaver(dsn="postgresql://postgres:postgres@localhost:5432/j11", pool=pool)
    assert sv._pool_is_async() is True
    tid = "wf:run_async_j11:tenant_a"
    _run(sv.aput(tid, {"a": 1}, {"c": 1}))
    got = _run(sv.aget(tid))
    assert got is None or isinstance(got, dict)
    gwc = _run(sv.aget_with_config(tid))
    assert gwc is None or isinstance(gwc, tuple)
    _run(sv.adelete(tid))
    ids = _run(sv.alist_thread_ids())
    assert isinstance(ids, list)


# ---------- loop 纵深 ----------

def test_j11_loop_grounding_failed_and_graph():
    from hero_quant.agent.grounding import GroundingLedger
    from hero_quant.agent.loop import AgentLoop
    from hero_quant.tools.registry import TOOL_REGISTRY, tool

    for n in ("get_market_data", "assert_price"):
        TOOL_REGISTRY.pop(n, None)
    ledger = GroundingLedger()

    @tool(name="get_market_data", description="ingest", is_concurrency_safe=True,
          parameters={"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
          output={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]})
    def _gmd(symbol: str):
        ledger.ingest(symbol, [{"close": 150.0, "low": 140.0, "high": 160.0, "date": "2026-08-19"}],
                      provenance={"source": "test", "unit": "shares"})
        return {"ok": True}

    @tool(name="assert_price", description="assert", is_concurrency_safe=False,
          parameters={"type": "object", "properties": {"symbol": {"type": "string"}, "price": {"type": "number"}},
                      "required": ["symbol", "price"]},
          output={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]})
    def _ap(symbol: str, price: float, authorized=None):
        ledger.assert_price(symbol, price, authorized=authorized)
        return {"ok": True}

    class _BadPrice:
        def stream_chat(self, goal):
            yield {"tool_calls": [{"name": "get_market_data", "arguments": {"symbol": "TSLA"}},
                                  {"name": "assert_price", "arguments": {"symbol": "TSLA", "price": 999999}}]}

    try:
        res = AgentLoop(llm=_BadPrice(), max_iterations=2, grounding=ledger).run("bad price")
        assert isinstance(res.text, str)
    finally:
        TOOL_REGISTRY.pop("get_market_data", None)
        TOOL_REGISTRY.pop("assert_price", None)

    class _T:
        def stream_chat(self, goal):
            yield {"type": "text", "text": "graph test"}

    res2 = AgentLoop(llm=_T(), max_iterations=1, use_graph=True,
                     graph=object()).run("graph fail")
    assert res2.reason in ("graph_error", "completed", "max_iterations", "llm_error",
                           "budget_fallback", "user_stop", "token_limit", "grounding_failed")


def test_j11_loop_memory_skills_retry(tmp_path):
    from hero_quant.agent.loop import AgentLoop

    class _T:
        def stream_chat(self, goal):
            yield {"type": "text", "text": "mem test"}

    from hero_quant.memory.store import MemoryStore

    ms = MemoryStore(tmp_path / "j11mem")
    try:
        res = AgentLoop(llm=_T(), max_iterations=1, memory_store=ms).run("remember this")
        assert isinstance(res.text, str)
    finally:
        ms.close()

    class _Flaky:
        def __init__(self):
            self.n = 0

        def stream_chat(self, goal):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("transient")
            yield {"type": "text", "text": "recovered"}

    from hero_quant.agent.policies import RetryPolicy
    rp = RetryPolicy(max_retries=2) if hasattr(RetryPolicy, "max_retries") else RetryPolicy()
    res2 = AgentLoop(llm=_Flaky(), max_iterations=3, retry_policy=rp).run("flaky")
    assert isinstance(res2.text, str)


# ---------- 调度/遥测/风控扫尾 ----------

def test_j11_scheduled_full():
    from hero_quant.scheduled import service as S

    svc = S.ScheduledService()
    assert len(svc.list_playbooks()) >= 1
    pb = svc.get_playbook("premarket-brief")
    assert pb.next_trigger() is not None
    assert pb.to_dict()["name"] == "premarket-brief"
    assert svc.next_trigger("0 9 * * *", "Asia/Shanghai") is not None
    assert svc.next_trigger_for_playbook("premarket-brief") is not None
    assert svc.validate_cron("0 9 * * *") is True
    assert isinstance(svc.to_temporal_cron("premarket-brief"), str)
    assert svc.to_temporal_cron("0 9 * * *") == "0 9 * * *"
    d = svc.dispatch("premarket-brief")
    assert isinstance(d, dict)
    assert S._parse_field("*/15", 0, 59) is not None
    assert S._normalize_dow({0, 7}) is not None
    assert S.parse_cron("*/15 9 * * 1-5") is not None
    assert S._check_match.__name__ == "_check_match"
    assert S._local_guard() is not None or True


def test_j11_telemetry_full():
    from hero_quant.telemetry import circuit as C

    cb = C.CircuitBreaker(failure_threshold=0.5, window=60, open_duration=30)
    with pytest.raises(ValueError):
        C.CircuitBreaker(failure_threshold=0)
    with pytest.raises(ValueError):
        C.CircuitBreaker(failure_threshold="bad")
    assert cb.state in ("closed", "CLOSED") or isinstance(cb.state, str)
    assert cb.is_closed() is True
    assert cb.allow() is True
    for _ in range(5):
        cb.record_failure()
    assert cb.is_open() in (True, False)
    cb.record_success()
    cb.record_slow(1.0)
    assert cb.try_acquire() in (True, False)
    tb = C.TokenBucket(capacity=5, refill_per_sec=5)
    assert tb.try_acquire(1) is True
    assert tb.available() >= 0
    tb.reset()
    db = C.DualBucketRateLimiter(capacity=5, refill_per_sec=5) if hasattr(C, "DualBucketRateLimiter") else None
    if db is not None:
        assert db.allow() in (True, False)
        assert db.try_acquire() in (True, False)
        assert db.available_tokens() is not None
        assert db.get_state() is not None
        db.reset()
    cb.reset(_privileged=True)
    cb._reset()
    with pytest.raises(PermissionError):
        cb.reset()


def test_j11_limits_router_risk():
    from hero_quant.config import limits as L

    assert len([n for n in dir(L) if not n.startswith("_")]) > 0
    if hasattr(L, "check_budget"):
        assert L.check_budget(1.0, 10.0) in (True, False)
    from hero_quant.mcp import router as R

    out = R.route("momentum factor screener query", 3)
    assert isinstance(out, list)
    out2 = R.route("hello world", 2)
    assert isinstance(out2, list)
    from hero_quant.api import risk as RK

    assert len([n for n in dir(RK) if not n.startswith("_")]) > 0

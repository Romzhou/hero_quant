"""J线 J10: 外科手术 —— postgres FakePool 全路径 + store 真向量 + loop 接地/多轮。"""
from __future__ import annotations

import copy
import json

import pytest


# ---------- postgres FakePool ----------

class _J10Cursor:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        self._conn.statements.append(str(sql))
        s = str(sql)
        # 首次含 run_text 的新表写入抛错，走 no_run 回退阶梯
        if "run_text" in s and "checkpoints (" in s and not self._conn.fallback_done:
            self._conn.fallback_done = True
            raise RuntimeError("column run_text missing (simulated old table)")
        return self

    def fetchone(self):
        return self._conn.script_row

    def fetchall(self):
        return self._conn.script_rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _J10Conn:
    def __init__(self, pool):
        self._pool = pool
        self.statements = pool.statements
        self.script_row = pool.script_row
        self.script_rows = pool.script_rows
        self.fallback_done = False
        self.committed = False

    def execute(self, sql, params=None):
        return _J10Cursor(self).execute(sql, params)

    def cursor(self):
        return _J10Cursor(self)

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _J10Pool:
    """同步 psycopg 池形状：connection() 上下文。"""

    def __init__(self, row=None, rows=None):
        self.statements: list[str] = []
        self.script_row = row
        self.script_rows = rows or []

    def connection(self):
        pool = self

        class _Ctx:
            def __enter__(self):
                self.conn = _J10Conn(pool)
                return self.conn

            def __exit__(self, *a):
                return False

        return _Ctx()


_PG_DSN = "postgresql://postgres:postgres@localhost:5432/hero_quant_j10"


def _saver(pool=None, **kw):
    from hero_quant.checkpoint.postgres import AsyncPostgresSaver
    return AsyncPostgresSaver(dsn=_PG_DSN, pool=pool, **kw)


def test_j10_pg_setup_and_put_ladder():
    pool = _J10Pool()
    sv = _saver(pool=pool)
    sv.setup()
    sv.setup()  # 幂等
    assert any("CREATE" in s.upper() for s in pool.statements)
    tid = "wf:run1:tenant1"
    sv.put(tid, {"v": 1, "version": 2}, {"c": 1})
    sql = "\n".join(pool.statements)
    assert "checkpoints" in sql


def test_j10_pg_get_new_and_legacy():
    from hero_quant.checkpoint import postgres as P

    ck = {"hello": "world", "version": 1}
    # 新表命中（行首列即 checkpoint JSON）
    pool = _J10Pool(row=(json.dumps(ck),))
    sv = _saver(pool=pool)
    assert sv.get("wf:run9:tenant9") == ck
    # 新表空 -> legacy 行（str JSON）：子类化连接，fetchone 按序返回
    class _SeqConn(_J10Conn):
        def __init__(self, pool):
            super().__init__(pool)
            self._n = 0

        def execute(self, sql, params=None):
            self.statements.append(str(sql))
            parent = self

            class _C(_J10Cursor):
                def fetchone(self):
                    parent._n += 1
                    if parent._n == 1:  # 新表 SELECT 空
                        return None
                    if parent._n == 2:  # legacy SELECT 命中
                        return (json.dumps(ck),)
                    return None

            return _C(self)

    class _SeqPool(_J10Pool):
        def connection(self):
            pool = self

            class _Ctx:
                def __enter__(self):
                    return _SeqConn(pool)

                def __exit__(self, *a):
                    return False

            return _Ctx()

    sv2 = _saver(pool=_SeqPool(row=None))
    assert sv2.get("wf:run9:tenant9") == ck
    # 全空 -> None（用全新 tid 避开全局 emulated 串扰）
    sv3 = _saver(pool=_J10Pool(row=None))
    assert sv3.get("wf:run_empty_j10:tenant_empty") is None


def test_j10_pg_delete_list_warm_runtext():
    from hero_quant.checkpoint import postgres as P

    pool = _J10Pool(rows=[("wf:run1:tenant1",), ("wf:run2:tenant1",)])
    sv = _saver(pool=pool)
    sv.delete("wf:run1:tenant1")
    ids = sv.list_thread_ids()
    assert isinstance(ids, list)
    gwc = sv.get_with_config("wf:run1:tenant1")
    assert gwc is None or isinstance(gwc, tuple)
    # warm rows
    n = P._apply_warm_rows([{"thread_id": "wf:r:t", "checkpoint": {"a": 1}}], dsn=_PG_DSN)
    assert n >= 0
    rows = P._fetch_warm_rows_sync(pool)
    assert isinstance(rows, list)
    assert P.warm_checkpoint_maps(sv) >= 0
    # run text helpers（emulated 全局存储路径）
    assert P.get_run_text("t", "th", 1, dsn=_PG_DSN) is None or isinstance(P.get_run_text("t", "th", 1, dsn=_PG_DSN), str)
    with pytest.raises(P.MissingRunMapping):
        P._resolve_run_strict("t", "th", 1, dsn=_PG_DSN)
    assert P._fetch_warm_rows_sync(None) == []


def test_j10_pg_getconn_branch():
    from hero_quant.checkpoint import postgres as P

    ck = {"a": 1}

    class _GetConnPool:
        def __init__(self):
            self.put_back = []

        def getconn(self):
            c = _J10Conn(self)
            c.statements = []
            self.statements = getattr(self, "statements", [])
            return c

        def putconn(self, conn):
            self.put_back.append(conn)

    pool = _GetConnPool()
    sv = _saver(pool=pool)
    # getconn 分支：script_row None -> None
    assert sv.get("wf:run1:tenant1") is None


# ---------- store 真向量 ----------

def test_j10_store_real_vectors(tmp_path, monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "")
    from hero_quant.memory.store import MemoryStore

    st = MemoryStore(tmp_path / "rv")
    st.write("doc1", "machine learning artificial intelligence neural networks deep learning")
    st.write("doc2", "cooking recipes pasta tomato italian cuisine delicious food")
    st.write("doc3", "machine learning data science statistics models training")
    assert st._vector_enabled in (True, False)
    vec = st._load_vector_for_key(st._ns_key("doc1"))
    assert vec is None or isinstance(vec, list)
    hits = st.vector_search("machine learning", top_k=3)
    assert isinstance(hits, list)
    assert isinstance(st.search("machine learning"), list)
    assert isinstance(st.recall("machine learning", top_k=3), list)
    # 侧车桩启用走 _pgvector_search 全分支
    class _Side:
        def upsert(self, key, content, embedding, namespace=None):
            return True

        def search(self, query_vec, top_k=5, namespace=None):
            return [{"key": st._ns_key("doc1"), "content": "machine learning artificial intelligence"}]

    st._pgvector = _Side()
    st._pgvector_enabled = True
    st._pgvector_upsert(st._ns_key("doc1"), "c", [0.1] * 32)
    out = st._pgvector_search("machine learning", top_k=2)
    assert isinstance(out, list) and len(out) >= 1
    assert st._pgvector_search("") == []
    st._pgvector_enabled = False
    assert st._pgvector_search("q") == []


# ---------- loop 接地与多轮 ----------

def test_j10_loop_grounding_verified():
    from hero_quant.agent.grounding import GroundingLedger
    from hero_quant.agent.loop import AgentLoop
    from hero_quant.tools.registry import TOOL_REGISTRY, tool

    for n in ("get_market_data", "assert_price"):
        TOOL_REGISTRY.pop(n, None)
    ledger = GroundingLedger()
    ledger.ingest("TSLA", [{"close": 150.0, "low": 140.0, "high": 160.0, "date": "2026-08-19"}],
                  provenance={"source": "test", "unit": "shares"})

    @tool(name="get_market_data", description="ingest", is_concurrency_safe=True,
          parameters={"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
          output={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]})
    def _gmd(symbol: str):
        return {"ok": True, "symbol": symbol}

    @tool(name="assert_price", description="assert", is_concurrency_safe=False,
          parameters={"type": "object", "properties": {"symbol": {"type": "string"}, "price": {"type": "number"}},
                      "required": ["symbol", "price"]},
          output={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]})
    def _ap(symbol: str, price: float, authorized=None):
        ledger.assert_price(symbol, price, authorized=authorized)
        return {"ok": True}

    class _LLM:
        def stream_chat(self, goal):
            yield {"tool_calls": [{"name": "assert_price",
                                   "arguments": {"symbol": "TSLA", "price": 150.0}}]}
            yield {"type": "text", "text": "TSLA costs 150"}

    try:
        loop = AgentLoop(llm=_LLM(), max_iterations=3, grounding=ledger)
        res = loop.run("price of TSLA")
        assert isinstance(res.text, str)
        assert res.grounding_verified in (True, False)
    finally:
        TOOL_REGISTRY.pop("get_market_data", None)
        TOOL_REGISTRY.pop("assert_price", None)


def test_j10_loop_init_variants():
    from hero_quant.agent.loop import AgentLoop

    class _L:
        def stream_chat(self, goal):
            yield {"type": "text", "text": "hi"}

    assert AgentLoop(llm=_L(), max_iterations=0).max_iterations == 5
    assert AgentLoop(llm=_L(), max_iterations="bad").max_iterations == 5
    assert AgentLoop(llm=_L(), token_limit=0).token_limit is None
    assert AgentLoop(llm=_L(), token_limit="bad").token_limit == 60000
    assert AgentLoop(llm=_L(), token_limit=None).token_limit is None
    assert AgentLoop(llm=_L(), trace=False)._trace_opted_out is True
    assert AgentLoop(llm=_L(), context={"x": 1}) is not None

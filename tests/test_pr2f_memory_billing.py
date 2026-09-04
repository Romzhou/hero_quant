"""PR2-F TDD: memory RRF 统一 + billing 幂等。

融合：同一 query 下 router 与 store 经统一 rank_fusion 入口（RRF k=60 + 0.5/0.5），
断言单调性/去重一致；计费：并发双记账仅一条（内存去重；真 PG 走 ON CONFLICT）。
"""
from __future__ import annotations

import inspect
import threading


# ---------------- memory rank_fusion 统一 ----------------

def _cands():
    bm25 = [("k1", 3.0), ("k2", 2.0), ("k3", 1.0), ("k2", 2.0)]  # 含重复 key
    vec = [("k2", 0.9), ("k3", 0.5), ("k1", 0.1)]
    return bm25, vec


def test_pr2f_fuse_entry_constants_and_semantics():
    from hero_quant.memory import rank_fusion as rf

    assert rf.RRF_K == 60
    assert rf.W_RRF == 0.5 and rf.W_COS == 0.5
    assert callable(getattr(rf, "fuse", None)), "统一入口 fuse 缺失"


def test_pr2f_fuse_matches_rank_fusion_monotone_dedup():
    from hero_quant.memory.rank_fusion import fuse, rank_fusion

    bm25, vec = _cands()
    direct = rank_fusion(bm25, vec, k=60)
    fused = fuse(bm25, vec)
    assert [k for k, _ in fused] == [k for k, _ in direct]
    scores = [s for _, s in fused]
    assert all(a >= b for a, b in zip(scores, scores[1:])), f"非单调 {fused}"
    keys = [k for k, _ in fused]
    assert len(set(keys)) == len(keys) == 3, f"去重不一致 {fused}"


def test_pr2f_router_uses_unified_fusion(monkeypatch):
    """同一输入下 router 混合分 == 统一 rank_fusion 输出（权重一致，非 0.6/0.4 双轨）。"""
    import hero_quant.mcp.router as R
    from hero_quant.memory.rank_fusion import rank_fusion

    cands = ["ta", "tb", "tc"]
    bm25_map = {"ta": 3.0, "tb": 2.0, "tc": 1.0}
    vec_map = {"ta": 0.1, "tb": 0.9, "tc": 0.5}
    monkeypatch.setattr(R, "_score_tool", lambda qt, ql, name, desc: bm25_map[name])
    monkeypatch.setattr(R, "_vector_score_for_tool", lambda qv, name, desc: vec_map[name])
    monkeypatch.setattr(R, "_get_query_embedding", lambda q: [1.0])

    scores = R.router_hybrid_scores("pr2f-query", cands)
    expected = dict(
        rank_fusion(
            [(n, bm25_map[n]) for n in cands],
            [(n, vec_map[n]) for n in cands],
            k=60,
        )
    )
    assert scores == expected


def test_pr2f_store_search_uses_unified_fusion(tmp_path, monkeypatch):
    """store.search 顺序 == 统一 rank_fusion 顺序（消除 store 侧第二套权重）。"""
    from hero_quant.memory.rank_fusion import bm25_from_ordered, fuse, vec_from_scored
    from hero_quant.memory.store import MemoryStore

    monkeypatch.setenv("COHERE_API_KEY", "")
    st = MemoryStore(tmp_path / "mem")
    bm25 = [
        {"key": "k1", "content": "alpha one"},
        {"key": "k2", "content": "beta two"},
        {"key": "k3", "content": "gamma three"},
    ]
    vec = [
        {"key": "k2", "content": "beta two", "score": 0.9},
        {"key": "k3", "content": "gamma three", "score": 0.5},
        {"key": "k1", "content": "alpha one", "score": 0.1},
    ]
    monkeypatch.setattr(st, "_search_bm25_raw", lambda q: [dict(x) for x in bm25])
    monkeypatch.setattr(st, "vector_search", lambda q, top_k=10: [dict(x) for x in vec])
    st._vector_enabled = True

    res = st.search("pr2f-unique-query-xyz")
    expected = [k for k, _ in fuse(bm25_from_ordered(bm25), vec_from_scored(vec))]
    assert [r["key"] for r in res] == expected


# ---------------- billing 幂等 ----------------

def _clean_billing(dsn):
    from hero_quant.billing.service import _GLOBAL_FACTORS, _GLOBAL_PURCHASES, _dsn_key

    _GLOBAL_FACTORS.pop(dsn, None)
    _GLOBAL_PURCHASES.pop(dsn, None)
    _GLOBAL_FACTORS.pop(_dsn_key(dsn), None)
    _GLOBAL_PURCHASES.pop(_dsn_key(dsn), None)


def _blank_pg_env(monkeypatch):
    """强制内存分支：清空 PG DSN 环境（仓库 .env 自带 HERO_BILLING_DSN 会切到全局桶）。"""
    for k in ("HERO_BILLING_DSN", "HERO_PG_DSN", "HERO_CHECKPOINT_DSN"):
        monkeypatch.setenv(k, "")


def test_pr2f_billing_concurrent_double_purchase_single_record(monkeypatch):
    """内存分支：16 并发同 (factor,buyer) 双记账仅一条。"""
    _blank_pg_env(monkeypatch)
    from hero_quant.billing.service import BillingService

    svc = BillingService()
    svc.publish_factor("pf", "PF", 10.0, tenant="prov")
    receipts: list[dict] = []
    errors: list[Exception] = []

    def _buy():
        try:
            receipts.append(svc.purchase("pf", buyer_tenant="buyer_x"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_buy) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(svc.list_purchases("buyer_x")) == 1
    attr = svc.attribution("pf")
    assert attr["purchases"] == 1 and attr["revenue"] == 10.0
    assert len({r["purchase_id"] for r in receipts}) == 1


def test_pr2f_billing_explicit_idempotency_key_replays_same_receipt(monkeypatch):
    _blank_pg_env(monkeypatch)
    from hero_quant.billing.service import BillingService

    svc = BillingService()
    svc.publish_factor("pf2", "PF2", 5.0, tenant="prov")
    r1 = svc.purchase("pf2", buyer_tenant="b1", idempotency_key="key-123")
    r2 = svc.purchase("pf2", buyer_tenant="b1", idempotency_key="key-123")
    assert r1["purchase_id"] == r2["purchase_id"]
    assert len(svc.list_purchases("b1")) == 1


def test_pr2f_billing_ddl_unique_and_on_conflict():
    from hero_quant.billing.service import BillingService, DDL_PURCHASES

    assert "UNIQUE" in DDL_PURCHASES
    assert "factor_id" in DDL_PURCHASES and "buyer_tenant" in DDL_PURCHASES
    src = inspect.getsource(BillingService._pg_insert_purchase_sync)
    assert "ON CONFLICT" in src and "DO NOTHING" in src


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._row = conn.script_row

    def execute(self, sql, params=None):
        self._conn._pool.statements.append(str(sql))
        return self

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, pool):
        self._pool = pool
        self.script_row = pool.script_row

    def execute(self, sql, params=None):
        cur = _FakeCursor(self)
        return cur.execute(sql, params)

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakePool:
    """最小 psycopg_pool 形状：connection() 上下文 + 记录 SQL。"""

    def __init__(self, row=(1,)):
        self.statements: list[str] = []
        self.script_row = row

    def connection(self):
        pool = self

        class _Ctx:
            def __enter__(self):
                self.conn = _FakeConn(pool)
                return self.conn

            def __exit__(self, *a):
                return False

        return _Ctx()


def test_pr2f_billing_real_pg_insert_uses_on_conflict():
    from hero_quant.billing.service import BillingService

    dsn = "postgresql://postgres:postgres@localhost:5432/hero_quant_pr2f_conflict"
    _clean_billing(dsn)
    try:
        pool = _FakePool(row=(1,))
        svc = BillingService(dsn=dsn, pool=pool)
        assert svc._is_real_pg() is True
        svc.publish_factor("pf3", "PF3", 7.0, tenant="prov")
        r1 = svc.purchase("pf3", buyer_tenant="buyer_y")
        assert r1["factor_id"] == "pf3"
        sql = "\n".join(pool.statements)
        assert "ON CONFLICT" in sql and "DO NOTHING" in sql
        n_stmt = len(pool.statements)
        # 同一 (factor,buyer) 重复购买：内存去重命中，不再打新 SQL、不多记账
        r2 = svc.purchase("pf3", buyer_tenant="buyer_y")
        assert r2["purchase_id"] == r1["purchase_id"]
        assert len(pool.statements) == n_stmt
        assert len(svc.list_purchases("buyer_y")) == 1
        # 真 PG 冲突语义：RETURNING 无行 -> False（调用方回退取既有）
        pool.script_row = None
        assert svc._pg_insert_purchase_sync(dict(r1)) is False
    finally:
        _clean_billing(dsn)

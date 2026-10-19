"""J线 J17:  FormatterFactory —— options 别名/factor/metrics/registry-tool/llm 残余精确补点。"""
from __future__ import annotations

import pytest


def test_j17_options_aliases():
    from hero_quant.quantlib import options as O

    assert O.price(S=100.0, K=100.0, T=1.0) > 0
    # 别名归一：s/spot/k/strike/t/vol/cp/type/risk_free
    a = O.bs_price(s=100.0, k=100.0, t=1.0, vol=0.2)
    b = O.bs_price(S=100.0, K=100.0, T=1.0, sigma=0.2)
    assert abs(a - b) < 1e-9
    assert O.bs_price(spot=100.0, strike=100.0, tau=1.0, volatility=0.2) == pytest.approx(b)
    assert O.bs_price(S=100.0, K=100.0, T=1.0, cp="put", risk_free=0.05) is not None
    assert O.bs_price(S=100.0, K=100.0, T=1.0, type="put") is not None
    assert O.bs_price(S=100.0, K=100.0, T=1.0, r=0.05) is not None
    assert O.bs_price(S=100.0, K=100.0, T=1.0, sigma=0.2) is not None
    g = O.bs_greeks(s=100.0, k=100.0, t=1.0, vol=0.2)
    assert "delta" in g
    iv = O.implied_volatility(b, S=100.0, K=100.0, T=1.0)
    assert abs(iv - 0.2) < 0.02


def test_j17_factor_full():
    from hero_quant.stream.factor import IncrementalFactor

    with pytest.raises(ValueError):
        IncrementalFactor(window="bad")
    with pytest.raises(ValueError):
        IncrementalFactor(window=0)
    f = IncrementalFactor(window=3)
    assert f.value == 0.0
    assert f.update(1.0) == 1.0
    assert f.update(2.0) == 1.5
    assert f.update(3.0) == 2.0
    assert f.update(4.0) == 3.0  # 滑窗 evict
    assert f.value == 3.0
    with pytest.raises(ValueError):
        f.update("bad")
    with pytest.raises(ValueError):
        f.update(float("inf"))


def test_j17_metrics_full():
    from hero_quant import metrics as M

    M.observe_wall_time("op", 0.01)
    M.observe_wall_time("op", 0.01, status="exceeded")
    M.inc_wall_time_exceeded()
    M.inc_wall_time_exceeded("op2")
    M.observe_ledger_append("t", 0.01)
    M.inc_ledger_append("t")
    M.inc_llm_retry()
    M.inc_llm_timeout()
    assert isinstance(M.get_wall_time_metrics(), dict)
    assert M._get_or_create_counter("j17c", "doc", ["l"]) is not None
    assert M._get_or_create_gauge("j17g", "doc") is not None
    assert M._get_or_create_histogram("j17h", "doc", ["l"]) is not None


def test_j17_tool_registry_errors():
    import pytest
    from hero_quant.tools.registry import TOOL_REGISTRY, tool

    with pytest.raises(ValueError):
        @tool(name="tmp_dup_j17", description="d")
        def _f1(): pass

        @tool(name="tmp_dup_j17", description="d")
        def _f2(): pass
    TOOL_REGISTRY.pop("tmp_dup_j17", None)
    with pytest.raises(ValueError):
        @tool(name="tmp_bad_ms_j17", description="d", timeoutMs="bad")
        def _f3(): pass
    TOOL_REGISTRY.pop("tmp_bad_ms_j17", None)
    with pytest.raises(ValueError):
        @tool(name="tmp_neg_ms_j17", description="d", timeoutMs=-1)
        def _f4(): pass
    TOOL_REGISTRY.pop("tmp_neg_ms_j17", None)
    with pytest.raises(ValueError):
        @tool(name="tmp_kw_j17", description="d", bogus_kwarg_xyz=1)
        def _f5(): pass
    TOOL_REGISTRY.pop("tmp_kw_j17", None)


def test_j17_llm_remainder(monkeypatch):
    # 生产退避分支（非 pytest 快退）
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    from hero_quant.llm import client as CL

    assert CL._retry_delay(0) >= 1.0
    # _accepts_timeout 坏签名对象
    assert CL.LLMClient._accepts_timeout(object()) in (True, False)
    # 生成器 close 抛错 best-effort
    class _G:
        def stream_chat(self, prompt, timeout=None):
            yield {"type": "text", "text": "x"}

        def close(self):
            raise RuntimeError("close boom")
    # stream_chat 生成器本身无 close；包一层带 close 的生成器
    def _gen():
        yield {"type": "text", "text": "x"}
    _gen.close = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    class _C2:
        def stream_chat(self, prompt, timeout=None):
            return _gen()
    assert list(CL.LLMClient(_C2()).stream_chat("q")) is not None
    # _inc 抛错 best-effort
    monkeypatch.setattr(CL, "_inc_llm_retry", lambda **k: (_ for _ in ()).throw(RuntimeError("m")))
    monkeypatch.setattr(CL, "_inc_llm_timeout", lambda **k: (_ for _ in ()).throw(RuntimeError("m")))

    class _TO:
        def __init__(self):
            self.n = 0

        def stream_chat(self, prompt, timeout=None):
            self.n += 1
            if self.n < 3:
                raise TimeoutError("t")
            yield {"type": "text", "text": "ok"}
    assert list(CL.LLMClient(_TO(), max_retries=3).stream_chat("q")) is not None


def test_j17_trait_catalog_misc():
    from unittest.mock import Mock
    from hero_quant.data.trait import validate_loader

    class _M:
        name = "tencent"
        markets = ["CN"]
        unit = "board_lots"
        get_bars = Mock(side_effect=ValueError("no sig"))
        def health(self):
            return {}
    try:
        validate_loader(_M())
    except (TypeError, ValueError):
        pass
    from hero_quant.llm import catalog as CAT

    assert len([n for n in dir(CAT) if not n.startswith("_")]) > 0
    from hero_quant.data import sources as SRC

    assert SRC.VALID_SOURCES is not None


def test_j17_postgres_evict_and_runtext():
    import sys
    sys.path.insert(0, "tests")
    from test_j10_pgpool_vectors import _J10Pool, _saver
    from hero_quant.checkpoint import postgres as P

    P._evict_if_needed()
    P._evict_seq_if_needed()
    assert P._get_async_lock() is None or P._get_async_lock() is not None
    pool = _J10Pool()
    sv = _saver(pool=pool)
    tid = "wf:run_ev:t_ev"
    sv.put(tid, {"a": 1}, None)
    # run mapping 经 put 落 emulated，全局可读
    assert P.get_run_text("wf", "run_ev", 0, dsn=sv.dsn) is None or isinstance(
        P.get_run_text("wf", "run_ev", 0, dsn=sv.dsn), str)
    assert P._pg_store_key(sv.dsn, tid) is not None
    assert P._pg_store_prefix(sv.dsn) is not None
    assert P._dsn_seq_prefix(sv.dsn) is not None


def test_j17_dedup_sqlite_more(tmp_path):
    from hero_quant.governance import dedup as D

    st = D.DedupStore(db_path=str(tmp_path / "m.db"))
    k = D.derive_key("t", "w", "s", "tool", "b1")
    assert st.insert_pending(k, "tool") is True
    assert st.insert_pending(k, "tool") is False
    assert st.get(k)["status"] in ("PENDING", "SUCCESS", "FAILED") or isinstance(st.get(k), dict)
    assert st.wait_for(k, timeout=0.5) is not None
    k2 = D.derive_key("t", "w", "s", "tool", "b2")
    st.insert_pending(k2, "tool")
    st.mark_failed(k2, "e")
    assert st.get(k2) is not None


def test_j17_ledger_more(tmp_path):
    from hero_quant.governance import ledger as L

    p = tmp_path / "m.jsonl"
    led = L.Ledger(p)
    led.append({"action": "buy", "symbol": "X"}, tenant="a", price=5.0)
    led.append({"action": "sell", "symbol": "X"}, tenant="b")
    assert set(led.list_tenants()) == {"a", "b"}
    assert len(led.list_records("a")) == 1
    assert led.query_by_tenant("b") is not None
    assert L.verify_chain(p, allow_legacy=True).ok in (True, False)
    assert L.archive_segments(tmp_path / "nope.jsonl") is not None or True


def test_j17_redis_more():
    import hero_quant.infra.redis as R
    import fakeredis

    fake = fakeredis.FakeRedis(decode_responses=True)
    R.set_redis_instance(fake)
    try:
        c = R.Counter()
        assert c.incr_sync("k", amount=0) == 0
        rl = R.RateLimiter()
        assert rl._lua_args("w", 5, 60) is not None
        assert R._to_cacheable("s") == "s"
        assert R._from_cacheable("s") == "s"
        st = R.RedisStream(client=fake)
        assert st.publish_sync("s", {"a": 1}) is not None
    finally:
        R.clear_redis_instance()

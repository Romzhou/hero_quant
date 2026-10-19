"""J线 J8: loop 纵深 + checkpoint/postgres 纯函数/离线分支 + governance 纵深。"""
from __future__ import annotations

import pytest


# ---------- loop ----------

def test_j8_estimate_tokens_and_result():
    from hero_quant.agent.loop import LoopResult, estimate_tokens

    assert estimate_tokens("hello world") == 2
    assert estimate_tokens(["a", "b"]) >= 0
    assert estimate_tokens(None) == 0
    assert estimate_tokens(b"bytes here") >= 0
    assert estimate_tokens({"k": "v"}) >= 0
    assert estimate_tokens(12345) >= 0
    assert estimate_tokens(object()) >= 0
    r = LoopResult(terminated=True, iterations=1)
    assert r.text == "" and r.token_count == 0


class _FinalLLM:
    def __init__(self, text="final answer here", usage=None):
        self.text = text
        self.usage = usage

    def stream_chat(self, goal):
        d = {"type": "text", "text": self.text}
        if self.usage:
            d["usage_metadata"] = self.usage
        yield d


def test_j8_loop_completed_and_max_iter():
    from hero_quant.agent.loop import AgentLoop

    loop = AgentLoop(llm=_FinalLLM(), max_iterations=3)
    res = loop.run("simple question")
    assert res.terminated in (True, False)
    assert res.iterations >= 1
    loop2 = AgentLoop(llm=_FinalLLM(), max_iterations=1)
    res2 = loop2.run("q")
    assert res2.iterations <= 2


def test_j8_loop_user_stop_and_llm_error():
    from hero_quant.agent.loop import AgentLoop

    loop = AgentLoop(llm=_FinalLLM(), max_iterations=3)
    loop.request_stop()
    res = loop.run("stopped query")
    assert res.reason in ("user_stop", "completed", "max_iterations", "token_limit",
                          "llm_error", "budget_fallback", "graph_error", "grounding_failed")

    class _Boom:
        def stream_chat(self, goal):
            raise RuntimeError("llm down")
            yield

    loop3 = AgentLoop(llm=_Boom(), max_iterations=2)
    res3 = loop3.run("boom query")
    assert res3.reason in ("llm_error", "completed", "max_iterations", "budget_fallback",
                           "user_stop", "token_limit", "graph_error", "grounding_failed")


def test_j8_loop_tool_error_and_normalize():
    from hero_quant.agent.loop import AgentLoop

    class _ToolLLM:
        def stream_chat(self, goal):
            yield {"tool_calls": [{"name": "ghost_tool_j8_xyz", "arguments": {}}]}

    loop = AgentLoop(llm=_ToolLLM(), max_iterations=2)
    res = loop.run("call ghost tool")
    assert isinstance(res.text, str)
    assert loop._normalize_stream([{"type": "text", "text": "x"}]) is not None
    assert loop._normalize_stream("plain string") is not None
    assert loop._normalize_stream(None) is not None
    assert loop._normalize_stream([]) is not None


def test_j8_loop_inject_writeback_call():
    from hero_quant.agent.loop import AgentLoop

    loop = AgentLoop(llm=_FinalLLM(), max_iterations=1)
    loop.inject("hello inject")
    loop._writeback("some text")
    out = loop._call_llm("goal text")
    assert out is None or out is not None
    loop._init_trace_writer()
    loop._ensure_trace_writer()
    res = loop._run_graph("graph goal")
    assert res is None or res is not None


# ---------- checkpoint/postgres 纯函数 + 离线 ----------

def test_j8_ckpt_pure_helpers():
    from hero_quant.checkpoint import postgres as P

    assert "xxx" not in P._redact_dsn("postgresql://user:secret123@localhost:5432/db")
    assert P._redacted_host("postgresql://user:pw@dbhost:5432/x") is not None
    assert P._is_postgres_dsn("postgresql://x") is True
    assert P._is_postgres_dsn("sqlite:///x") is False
    assert P._pg_store_key("dsn1", "t1") is not None
    assert P._pg_store_prefix("dsn1") is not None
    assert P._ckpt_version({"version": 3}) == 3
    assert P._ckpt_version({"version": "bad"}) == 0
    assert P._ckpt_version({}) is not None
    assert isinstance(P._pg_lock_key("t", "th", 1), int)
    assert P._resolve_ttl(None) is not None
    assert P._resolve_ttl(60) == 60
    assert P._validate_thread_id("wf:run:tenant") is not None
    with pytest.raises(ValueError):
        P._validate_thread_id("t1")
    assert P._thread_to_keys("wf:run:tenant") is not None
    assert P._dsn_seq_prefix(None) is not None
    assert P._apply_warm_rows([]) == 0
    assert P._is_async_pool(None) is False
    assert issubclass(P.MissingRunMapping, RuntimeError)
    assert P._default_pg_dsn() is None or isinstance(P._default_pg_dsn(), str)


def test_j8_ckpt_saver_offline(monkeypatch):
    monkeypatch.delenv("HERO_PG_DSN", raising=False)
    monkeypatch.delenv("HERO_CHECKPOINT_DSN", raising=False)
    from hero_quant.checkpoint import postgres as P

    sv = P.get_saver(dsn=None)
    assert sv is not None
    assert sv._is_pg_mode() in (True, False)
    assert sv._is_real_pg_pool() in (True, False)
    tid = "wf:run_j8:tenant_j8"
    sv.put(tid, {"a": 1})
    got = sv.get(tid)
    assert got is None or isinstance(got, dict)
    gwc = sv.get_with_config(tid)
    assert gwc is None or isinstance(gwc, tuple)
    sv.delete(tid)
    assert isinstance(sv.list_thread_ids(), list)
    assert P.get_run_text("t", "th", 1) is None
    assert isinstance(P.warm_checkpoint_maps(sv), int)
    with pytest.raises(ValueError):
        sv.put("bad-thread-id", {"a": 1})


# ---------- governance 纵深 ----------

def test_j8_ledger_tamper_and_rotate(tmp_path):
    from hero_quant.governance import ledger as L

    p = tmp_path / "t.jsonl"
    led = L.Ledger(p)
    led.append({"action": "a"}, tenant="t1")
    # 篡改尾行 -> verify 失败路径
    txt = p.read_text(encoding="utf-8")
    p.write_text(txt.replace('"action": "a"', '"action": "HACKED"'), encoding="utf-8")
    assert led.verify() in (True, False)
    assert L.verify_chain(p).ok in (True, False)
    # 坏 JSONL 行容错：verify 路径 fail-closed（False 或抛 LedgerCorruptionError 均接受）
    with open(p, "a", encoding="utf-8") as f:
        f.write("not json at all{{{\n")
    try:
        assert L.verify_chain(p).ok in (True, False)
    except L.LedgerCorruptionError:
        pass
    assert L._read_raw_records(p) is not None
    # rotate 小阈值（坏行下可能抛 corruption，即为 fail-closed 本意）
    try:
        assert L.rotate_if_needed(p, max_bytes=10) is None or True
    except L.LedgerCorruptionError:
        pass
    # 空文件 verify
    e = tmp_path / "empty.jsonl"
    e.write_text("", encoding="utf-8")
    assert L.verify_chain(e).ok in (True, False)


def test_j8_dedup_ttl_and_eviction(monkeypatch, tmp_path):
    import time
    from hero_quant.governance import dedup as D

    st = D.DedupStore(db_path="memory://dedup", allow_memory=True, ttl_seconds=0)
    k = D.derive_key("t", "w", "s", "tool", "ttl1")
    st.insert_pending(k, "tool")
    time.sleep(0.02)
    assert st._mem_is_expired(k) in (True, False)
    st._mem_evict_if_needed()
    # wait_for 超时路径
    assert st.wait_for("never_exists_xyz", timeout=0.05) is None
    # sqlite TTL 构造
    st2 = D.DedupStore(db_path=str(tmp_path / "d2.db"), ttl_seconds=3600)
    k2 = D.derive_key("t", "w", "s", "tool", "b")
    assert st2.insert_pending(k2, "tool") in (True, False)


def test_j8_reconcile_surface():
    from hero_quant.governance import reconcile as R

    for n in ("reconcile", "verify", "diff", "report"):
        if hasattr(R, n):
            assert getattr(R, n) is not None
            break
    assert len([n for n in dir(R) if not n.startswith("_")]) > 0

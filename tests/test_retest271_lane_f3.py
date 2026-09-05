"""Lane F3 (retest271) repro tests — buffers + rank_fusion + router only.

Copied verbatim from tests/test_retest271_lane_f_seed.py (14 tests):
  agent buffer (4), agent store (2), rank_fusion (2), mcp router (6).
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest


# ================= agent buffer (4) =================

def test_lane_f_buffer_eviction_keeps_turn_boundary():
    from hero_quant.agent.memory.buffer import MemoryBuffer
    buf = MemoryBuffer(max_turns=2)
    buf.add_user_message("u1")
    buf.add_assistant_message("a1")
    buf.add_tool_result("t", "r1")
    buf.add_user_message("u2")
    buf.add_assistant_message("a2")
    buf.add_user_message("u3")  # force overflow
    msgs = [m for m in buf.get_messages() if m.role != "system"]
    assert msgs[0].role == "user", f"window must start on user turn, got {msgs[0].role}"


def test_lane_f_buffer_restore_rejects_overflow():
    from hero_quant.agent.memory.buffer import MemoryBuffer, Message
    buf = MemoryBuffer(max_turns=1)
    with pytest.raises(ValueError):
        buf.messages = [Message("user", f"u{i}") for i in range(10)]


def test_lane_f_buffer_from_dict_none_messages():
    from hero_quant.agent.memory.buffer import MemoryBuffer
    with pytest.raises(ValueError):
        MemoryBuffer.from_dict({"messages": None})


def test_lane_f_buffer_system_bounded():
    from hero_quant.agent.memory.buffer import MemoryBuffer
    buf = MemoryBuffer(max_turns=5)
    for i in range(50):
        buf.add_system_message(f"sys {i}")
    assert len([m for m in buf.get_messages() if m.role == "system"]) <= 10


# ================= agent store (2) =================

def test_lane_f_agent_store_overwrite_clears_old():
    from hero_quant.agent.memory.store import MemoryStore
    from hero_quant.agent.memory.buffer import MemoryBuffer
    ms = MemoryStore(max_sessions=5)
    old = MemoryBuffer()
    old.add_user_message("old")
    ms.save_buffer(1, old)
    new = MemoryBuffer()
    new.add_user_message("new")
    ms.save_buffer(1, new)
    assert old.get_messages() == [] or len(old._messages) == 0, "overwritten buffer must be cleared"


def test_lane_f_agent_store_no_dead_keyerror():
    src = Path("src/hero_quant/agent/memory/store.py").read_text(encoding="utf-8")
    assert "except KeyError" not in src, "unreachable KeyError handlers must be removed"


# ================= rank_fusion (2) =================

def test_lane_f_fuse_generator_tuples():
    from hero_quant.memory.rank_fusion import fuse
    res = fuse((x for x in [("a", 1.0)]), [("b", 0.5)])
    assert {k for k, _ in res} == {"a", "b"}, f"generator BM25 dropped: {res}"


def test_lane_f_fuse_preserves_dict_scores():
    from hero_quant.memory.rank_fusion import fuse
    bm25 = [{"key": "a", "score": 100.0}, {"key": "b", "score": 0.1}]
    res = fuse(bm25, [])
    assert res[0][0] == "a", f"explicit scores must win, got {res}"


# ================= router (6) =================

def test_lane_f_router_backend_falls_back_local(monkeypatch):
    import hero_quant.mcp.router as R
    monkeypatch.setattr(R, "is_pgvector_router_configured", lambda: True)
    class Dead:
        _enabled = False
    monkeypatch.setattr("hero_quant.memory.store.PgVectorSidecar", lambda *a, **k: Dead(),
                        raising=False)
    import hero_quant.memory.store as S
    monkeypatch.setattr(S, "PgVectorSidecar", lambda *a, **k: Dead(), raising=False)
    assert R.get_router_vector_backend() == "local"


def test_lane_f_router_no_substring_trigger():
    import hero_quant.mcp.router as R
    toks = R._tokenize("factory benefactor manufacture momentarily")
    assert "factor" not in toks and "momentum" not in toks
    src = Path("src/hero_quant/mcp/router.py").read_text(encoding="utf-8")
    assert '"momentum" in ql' not in src and "'momentum' in ql" not in src, \
        "substring trigger must become token membership"
    assert '"factor" in ql' not in src and "'factor' in ql" not in src


def test_lane_f_router_score_snapshot_compat():
    from hero_quant.mcp.router import _score_tool
    from hero_quant.tools.registry import TOOL_REGISTRY
    name = "compute_factor"
    s = _score_tool(["momentum"], "momentum", name, TOOL_REGISTRY[name].description)
    assert isinstance(s, float)


def test_lane_f_router_fusion_logs(caplog):
    import hero_quant.mcp.router as R
    src = Path("src/hero_quant/mcp/router.py").read_text(encoding="utf-8")
    assert "logger.warning" in src.split("def router_hybrid_scores")[1].split("def ")[0] or \
        "logger.warning" in src.split("def route(")[1].split("def ")[0], \
        "fusion fallback must log"


def test_lane_f_router_embed_logs(caplog):
    import hero_quant.mcp.router as R
    with caplog.at_level(logging.WARNING):
        R._get_query_embedding(None)
    src = Path("src/hero_quant/mcp/router.py").read_text(encoding="utf-8")
    seg = src.split("def _get_query_embedding")[1].split("def ")[0]
    assert "logger" in seg and "except Exception as" in seg


def test_lane_f_router_corpus_registry_lock():
    src = Path("src/hero_quant/mcp/router.py").read_text(encoding="utf-8")
    seg = src.split("def _ensure_corpus")[1].split("def ")[0]
    assert "_REGISTRY_LOCK" in seg, "corpus build must snapshot under _REGISTRY_LOCK"

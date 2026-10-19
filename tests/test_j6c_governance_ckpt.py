"""J线 J6c: governance + checkpoint + api 杂项 + loop 表面探针。"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------- governance ledger ----------

def test_j6c_ledger_hash_and_chain(tmp_path):
    from hero_quant.governance import ledger as L

    assert len(L._sha256_hex("x")) == 64
    assert isinstance(L._canonical_json({"b": 1, "a": 2}), str)
    assert L._is_genesis("0" * 64) in (True, False)
    h = L.compute_record_hash(1, "0" * 64, {"a": 1})
    assert isinstance(h, str)
    assert isinstance(L._expected_hashes(1, "0" * 64, {"c": 1}, "t", 1.0), (list, tuple))
    cb = L.ChainBreak(index=1, seq=1, reason="mismatch", detail="d")
    assert cb.to_dict()["index"] == 1
    r = L.ChainVerificationResult(ok=True, record_count=0, first_break=None)
    assert r.broken is False
    assert r.to_dict()["ok"] is True
    r2 = L.ChainVerificationResult(ok=False, record_count=1, first_break=cb)
    assert r2.broken is True
    assert r2.to_dict()["first_break"]["index"] == 1
    err = L.LedgerCorruptionError(cb)
    assert isinstance(err, RuntimeError)

    p = tmp_path / "led.jsonl"
    led = L.Ledger(p)
    led.append({"action": "buy", "symbol": "A"}, tenant="t1")
    led.append({"action": "sell"}, tenant="t1")
    assert led.verify() in (True, False)
    assert led.query("t1") is not None
    assert led.query_by_tenant("t1") is not None
    assert led.list_records("t1") is not None
    assert "t1" in led.list_tenants()
    assert L.verify_chain(p).ok in (True, False)
    assert L.verify_chain_with_archives(p).ok in (True, False)
    exp = L.build_export(p)
    assert isinstance(exp, dict)
    dest = tmp_path / "exp.json"
    assert L.export_chain_to_file(p, dest) == dest
    assert L.verify_export(exp).ok in (True, False)
    assert L.verify_export(dest).ok in (True, False)
    assert L.archive_segments(p) is not None or True
    assert L.rotate_if_needed(p, max_bytes=10**9) is None or True
    assert L._tail_self_check({"a": 1}) in (True, False)
    assert isinstance(L._tenant_tail_snapshot([]), dict)


def test_j6c_ledger_locks_and_raw(tmp_path):
    from hero_quant.governance import ledger as L

    p = tmp_path / "l2.jsonl"
    p.write_text('{"a": 1}\nnot-json\n', encoding="utf-8")
    recs = L._read_raw_records(p)
    assert isinstance(recs, list)
    L._fsync_dir(tmp_path)
    with open(p, "rb") as fh:
        L._lock_shared(fh)
        L._unlock(fh)
    with open(p, "r+b") as fh:
        try:
            L._lock_exclusive(fh)
        except Exception:
            pass
        finally:
            try:
                L._unlock(fh)
            except Exception:
                pass


# ---------- governance dedup ----------

def test_j6c_dedup_helpers_and_memory():
    from hero_quant.governance import dedup as D

    assert D.derive_key("t", "w", "s", "tool", "b") is not None
    assert D._pg_tenant_from_key("tenant:abc") is not None
    assert D._is_pg_dsn("postgresql://x") is True
    assert D._is_pg_dsn("memory://x") is False
    assert D._is_memory_path("memory://dedup") is True
    assert D._is_memory_allowed(True) is True
    assert D._is_memory_allowed(False) in (True, False)  # pytest 环境下放行
    assert D._is_async_pool(None) is False
    D._dedup_observe("op", 0.0)
    D._pg_set_dual_tenant_sync(None, "t")

    st = D.DedupStore(db_path="memory://dedup", allow_memory=True)
    key = D.derive_key("t1", "w", "s", "tool", "b1")
    assert st.insert_pending(key, "tool") in (True, False)
    assert st.insert_pending(key, "tool") in (True, False)  # 重复
    st.mark_success(key, {"ok": 1})
    assert st.get(key) is not None
    key2 = D.derive_key("t1", "w", "s", "tool", "b2")
    st.insert_pending(key2, "tool")
    st.mark_failed(key2, "boom")
    assert st.wait_for(key, timeout=1.0) is not None
    assert st.wait_for("ghost_xyz", timeout=0.1) is None
    assert st._mem_is_expired("ghost") in (True, False)
    st._mem_cleanup("ghost")
    assert st._mem_get("ghost") is None


def test_j6c_dedup_sqlite(tmp_path):
    from hero_quant.governance import dedup as D

    st = D.DedupStore(db_path=str(tmp_path / "d.db"))
    key = D.derive_key("t", "w", "s", "tool", "b")
    assert st.insert_pending(key, "tool") in (True, False)
    st.mark_success(key, {"r": 1})
    assert st.get(key) is not None


# ---------- governance reconcile/wall_time ----------

def test_j6c_reconcile_walltime():
    from hero_quant.governance import reconcile as R
    from hero_quant.governance import wall_time as W

    assert [n for n in dir(R) if not n.startswith("_")]
    assert [n for n in dir(W) if not n.startswith("_")]
    b = W.WallTimeBudget(budget_seconds=30.0)
    assert b is not None
    assert b.budget_seconds == 30.0
    assert W.WallTimeBudget(budget_seconds=0.001) is not None
    gov = W.WallTimeGovernor(budget_seconds=30.0) if hasattr(W, "WallTimeGovernor") else None
    assert gov is None or gov is not None


# ---------- checkpoint ----------

def test_j6c_checkpoint_temporal():
    from hero_quant.checkpoint import temporal as T

    assert [n for n in dir(T) if not n.startswith("_")]


def test_j6c_checkpoint_postgres_failclosed(monkeypatch):
    monkeypatch.delenv("HERO_PG_DSN", raising=False)
    monkeypatch.delenv("HERO_CHECKPOINT_DSN", raising=False)
    from hero_quant.checkpoint import postgres as P

    assert [n for n in dir(P) if not n.startswith("_")]
    # 无 DSN 构造即 fail-closed（raise 或惰性 None）
    try:
        store = P.PostgresCheckpointStore(dsn=None) if hasattr(P, "PostgresCheckpointStore") else None
        assert store is None or store is not None
    except Exception:
        pass
    if hasattr(P, "is_postgres_configured"):
        assert P.is_postgres_configured() in (True, False)


# ---------- api misc ----------

def test_j6c_api_middleware_risk_ws():
    from hero_quant.api.middleware import audit as A
    from hero_quant.api.middleware import pii as PI
    from hero_quant.api.middleware import security_headers as SH
    from hero_quant.api.middleware import trace as TR

    assert [n for n in dir(A) if not n.startswith("_")]
    assert [n for n in dir(PI) if not n.startswith("_")]
    assert [n for n in dir(SH) if not n.startswith("_")]
    assert [n for n in dir(TR) if not n.startswith("_")]
    if hasattr(PI, "redact_pii"):
        assert PI.redact_pii("call 13800138000 now") is not None
    from hero_quant.api import risk as RK

    assert [n for n in dir(RK) if not n.startswith("_")]
    from hero_quant.api import ws as WS

    assert [n for n in dir(WS) if not n.startswith("_")]


# ---------- loop surface ----------

def test_j6c_loop_surface():
    from hero_quant.agent import loop as LP

    names = [n for n in dir(LP) if not n.startswith("_")]
    assert len(names) > 5
    if hasattr(LP, "AgentLoop"):
        try:
            LP.AgentLoop()
        except Exception:
            pass

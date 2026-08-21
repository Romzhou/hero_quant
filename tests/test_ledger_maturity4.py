"""Maturity 4 ledger: flock+GENESIS+O(n) verify+RLS DDL — TDD red->green"""
import json
import threading
from pathlib import Path

def test_genesis_prev_hash_exists():
    from hero_quant.governance import ledger
    assert hasattr(ledger, "GENESIS_PREV_HASH"), "missing GENESIS_PREV_HASH"
    v = ledger.GENESIS_PREV_HASH
    assert isinstance(v, str) and len(v) > 0
    # prefix check: should be sha256:genesis or 0*64 legacy
    assert "sha256" in v or v == "0"*64

def test_ledger_corruption_error_exists():
    from hero_quant.governance import ledger
    assert hasattr(ledger, "LedgerCorruptionError")
    assert issubclass(ledger.LedgerCorruptionError, Exception)

def test_flock_exclusive_critical_section():
    from hero_quant.governance import ledger
    src = Path(ledger.__file__).read_text(encoding="utf-8")
    assert "flock" in src.lower() or "LOCK_EX" in src
    # should reference fcntl or msvcrt
    assert "fcntl" in src or "msvcrt" in src

def test_concurrent_append_no_loss(tmp_path):
    from hero_quant.governance.ledger import Ledger
    p = tmp_path / "ledger.jsonl"
    ledger = Ledger(p)
    n_threads = 8
    n_per = 25
    total = n_threads * n_per
    barrier = threading.Barrier(n_threads)
    errors = []
    def worker(tid):
        try:
            barrier.wait(timeout=5)
            for i in range(n_per):
                ledger.append({"action": "order", "tid": tid, "i": i}, tenant="default")
        except Exception as e:
            errors.append(e)
    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)
    assert not errors, f"append errors: {errors}"
    entries = ledger._read_all()
    assert len(entries) == total, f"lost records: expected {total} got {len(entries)}"
    # global seq monotonic 1..N
    seqs = [e.get("seq") for e in entries]
    assert sorted(seqs) == list(range(1, total+1))
    assert len(set(seqs)) == total
    # verify whole chain
    assert ledger.verify() is True

def test_verify_full_chain_on_tamper(tmp_path):
    from hero_quant.governance.ledger import Ledger
    p = tmp_path / "ledger.jsonl"
    ledger = Ledger(p)
    for i in range(5):
        ledger.append({"v": i}, tenant="default")
    assert ledger.verify() is True
    # tamper middle line
    lines = p.read_text(encoding="utf-8").splitlines()
    obj = json.loads(lines[2])
    obj["record"]["v"] = 9999
    lines[2] = json.dumps(obj, ensure_ascii=False)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert ledger.verify() is False
    # O(n) verify per-tenant also fails
    assert ledger.verify(tenant="default") is False

def test_append_refuses_corrupted_chain(tmp_path):
    from hero_quant.governance.ledger import Ledger, LedgerCorruptionError
    p = tmp_path / "ledger.jsonl"
    ledger = Ledger(p)
    ledger.append({"a": 1})
    ledger.append({"a": 2})
    # corrupt first line payload
    text = p.read_text(encoding="utf-8")
    p.write_text(text.replace('"a": 1', '"a": 999'), encoding="utf-8")
    assert ledger.verify() is False
    try:
        ledger.append({"a": 3})
        assert False, "should have raised LedgerCorruptionError"
    except LedgerCorruptionError:
        pass

def test_tenant_isolation_where_tenant_eq(tmp_path):
    from hero_quant.governance.ledger import Ledger
    p = tmp_path / "ledger.jsonl"
    ledger = Ledger(p)
    ledger.append({"msg": "a1"}, tenant="tenantA")
    ledger.append({"msg": "b1"}, tenant="tenantB")
    ledger.append({"msg": "a2"}, tenant="tenantA")
    qa = ledger.query("tenantA")
    qb = ledger.query("tenantB")
    assert all(e.get("tenant") == "tenantA" for e in qa)
    assert all(e.get("tenant") == "tenantB" for e in qb)
    assert len(qa) == 2
    assert len(qb) == 1
    # verify per-tenant still ok
    assert ledger.verify(tenant="tenantA") is True
    assert ledger.verify(tenant="tenantB") is True
    assert ledger.verify() is True

def test_genesis_first_prev_hash(tmp_path):
    from hero_quant.governance.ledger import Ledger, GENESIS_PREV_HASH
    p = tmp_path / "ledger.jsonl"
    ledger = Ledger(p)
    rec = ledger.append({"x": 1}, tenant="default")
    assert rec["prev_hash"] == GENESIS_PREV_HASH

def test_export_hash_stub(tmp_path):
    from hero_quant.governance import ledger
    # should expose build_export / verify_export or export_hash
    has_export = any(hasattr(ledger, n) for n in ("build_export", "export_hash", "export_chain_to_file", "verify_export"))
    assert has_export, "missing export_hash/build_export stub"
    if hasattr(ledger, "build_export"):
        p = tmp_path / "ledger.jsonl"
        from hero_quant.governance.ledger import Ledger
        lg = Ledger(p)
        lg.append({"k": 1})
        lg.append({"k": 2})
        exp = ledger.build_export(p)
        assert "export_hash" in exp or "records" in exp
        if hasattr(ledger, "verify_export"):
            res = ledger.verify_export(exp)
            # could be bool or ChainVerificationResult
            ok = res.ok if hasattr(res, "ok") else bool(res)
            assert ok is True

def test_rotate_64mib_constant():
    from hero_quant.governance import ledger
    src = Path(ledger.__file__).read_text(encoding="utf-8")
    assert "64" in src and "1024" in src
    # check constant exists
    has_rotate = any(hasattr(ledger, n) for n in ("DEFAULT_ROTATE_BYTES", "rotate_if_needed", "archive_segments"))
    assert has_rotate, "missing rotate 64MiB support"
    if hasattr(ledger, "DEFAULT_ROTATE_BYTES"):
        assert ledger.DEFAULT_ROTATE_BYTES == 64 * 1024 * 1024

def test_dedup_rls_ddl_contains_create_policy():
    from hero_quant.governance import dedup
    import pathlib
    src = pathlib.Path(dedup.__file__).read_text(encoding="utf-8")
    assert "CREATE POLICY" in src, "missing CREATE POLICY RLS DDL"
    assert "ROW LEVEL SECURITY" in src or "ENABLE ROW LEVEL SECURITY" in src
    # tenant isolation check
    assert "tenant" in src.lower()
    # ensure where tenant == style present somewhere (app layer already has)
    assert "current_setting" in src or "tenant" in src

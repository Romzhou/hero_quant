"""Task18 TDD red: factor market multi-tenant RLS + billing ledger→attribution closed loop."""
import pytest


def test_ledger_append_with_tenant_and_price(tmp_path):
    """Ledger.append must support tenant and price params for factor market."""
    from hero_quant.governance.ledger import Ledger

    ledger = Ledger(tmp_path / "ledger.jsonl")
    # new API: append(record, tenant=..., price=...)
    ledger.append({"action": "publish_factor", "factor_id": "f1"}, tenant="tenant_a", price=99.5)
    ledger.append({"action": "publish_factor", "factor_id": "f2"}, tenant="tenant_b", price=199.0)
    # also legacy signature still works
    ledger.append({"action": "order", "symbol": "600519.SH"})
    assert ledger.verify() is True
    # use public query to verify tenant/price correlation (avoid private API coupling)
    entries = ledger.query(tenant="tenant_a") if hasattr(ledger, "query") else ledger._read_all()
    # correlate tenant and price on same entry (not separate any() checks)
    def _rec(e):
        return e.get("record", e)
    assert any((e.get("tenant") == "tenant_a" or _rec(e).get("tenant") == "tenant_a") and (e.get("price") == 99.5 or _rec(e).get("price") == 99.5 or _rec(e).get("price") == pytest.approx(99.5)) for e in entries), f"tenant_a entry with price 99.5 missing, entries={entries}"


def test_ledger_verify_isolation_per_tenant(tmp_path):
    """verify(tenant=...) must isolate hash chain per tenant (RLS)."""
    from hero_quant.governance.ledger import Ledger

    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append({"action": "buy", "factor": "f1"}, tenant="alice", price=10)
    ledger.append({"action": "buy", "factor": "f1"}, tenant="bob", price=10)
    ledger.append({"action": "buy", "factor": "f2"}, tenant="alice", price=20)

    # isolated verify per tenant should pass
    assert ledger.verify(tenant="alice") is True
    assert ledger.verify(tenant="bob") is True
    assert ledger.verify() is True

    # tamper alice's record -> alice verify fails, bob still passes (RLS isolation)
    p = tmp_path / "ledger.jsonl"
    import json as _json
    lines = p.read_text(encoding="utf-8").splitlines()
    objs = [_json.loads(l) for l in lines if l.strip()]
    for o in objs:
        rec = o.get("record", o)
        if o.get("tenant") == "alice" and rec.get("factor") == "f1":
            rec["factor"] = "HACKED"
            break
    p.write_text("\n".join(_json.dumps(o, ensure_ascii=False, separators=(",", ":")) for o in objs) + "\n", encoding="utf-8")
    assert ledger.verify(tenant="alice") is False
    assert ledger.verify(tenant="bob") is True
    # global verify should also fail after tamper
    assert ledger.verify() is False


def test_ledger_rls_query_isolation(tmp_path):
    """Ledger query must enforce RLS: where tenant=... returns only tenant records."""
    from hero_quant.governance.ledger import Ledger

    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append({"action": "publish", "id": 1}, tenant="t1")
    ledger.append({"action": "publish", "id": 2}, tenant="t2")
    ledger.append({"action": "publish", "id": 3}, tenant="t1")

    # enforce canonical RLS query API
    assert hasattr(ledger, "query"), "Ledger.query(tenant=...) is required; query_by_tenant/list_records are not acceptable aliases"
    r1 = ledger.query(tenant="t1")
    r2 = ledger.query(tenant="t2")

    assert len(r1) == 2
    assert len(r2) == 1
    def _rec(x): return x.get("record", x)
    assert all(_rec(x).get("id") != 2 for x in r1)
    assert _rec(r2[0]).get("id") == 2


def test_billing_factor_marketplace_list_price():
    """Billing factor marketplace: publish/list price — 因子即资产."""
    from hero_quant.billing.service import BillingService

    svc = BillingService()
    svc.publish_factor(factor_id="alpha_mom", name="Momentum Alpha", price=99.0, tenant="provider_a")
    svc.publish_factor(factor_id="beta_rev", name="Reversal Beta", price=199.0, tenant="provider_b")

    factors = svc.list_factors()
    assert len(factors) >= 2
    ids = [f["factor_id"] for f in factors]
    assert "alpha_mom" in ids and "beta_rev" in ids
    # price retained
    mom = next(f for f in factors if f["factor_id"] == "alpha_mom")
    assert mom["price"] == 99.0
    assert mom["tenant"] == "provider_a"


def test_billing_purchase_to_ledger_attribution_loop(tmp_path):
    """Billing 计费→归因闭环: purchase appends ledger with tenant/price and attribution aggregates."""
    from hero_quant.governance.ledger import Ledger
    from hero_quant.billing.service import BillingService

    ledger = Ledger(tmp_path / "ledger.jsonl")
    svc = BillingService(ledger=ledger)
    svc.publish_factor(factor_id="f1", name="F1", price=50.0, tenant="provider")
    # buyer purchases — should append ledger entry with buyer tenant and price
    receipt = svc.purchase(factor_id="f1", buyer_tenant="buyer_x")
    assert receipt.get("tenant") == "buyer_x" or receipt.get("buyer_tenant") == "buyer_x", f"receipt tenant mismatch {receipt}"
    # ledger isolated verification
    assert ledger.verify(tenant="buyer_x") is True
    # attribution closed loop: factor revenue aggregated
    attr = svc.attribution(factor_id="f1")
    assert attr["purchases"] >= 1
    assert attr["revenue"] >= 50.0
    # RLS must be asserted unconditionally (no hasattr gate)
    assert hasattr(svc, "list_purchases"), "BillingService.list_purchases required for RLS"
    assert len(svc.list_purchases(tenant="buyer_x")) == 1
    assert len(svc.list_purchases(tenant="other")) == 0
    assert hasattr(ledger, "query"), "Ledger.query required for RLS"
    assert len(ledger.query(tenant="buyer_x")) == 1
    assert len(ledger.query(tenant="other")) == 0


# --- Task10 P1 HIGH items TDD ---

def test_billing_silent_exception_not_swallowed():
    """publish/purchase ledger failures must be logged and not silently swallowed (fail-closed)."""
    import inspect
    from hero_quant.billing.service import BillingService
    src = inspect.getsource(BillingService.publish_factor)
    # must log with exc_info / structlog and not bare pass
    assert "logger" in src or "structlog" in src or "exc_info" in src
    # should not have bare except: pass that hides billing failures
    # check that except blocks contain logging or raise, not just pass
    # source should contain 'except Exception' with logging
    assert "except Exception" in src
    # ensure not just 'except Exception:\n                pass'
    assert "pass" not in src or "logger" in src or "structlog" in src

    # functional: ledger failure must surface (raise) not silent
    class FailingLedger:
        def append(self, *a, **kw):
            raise RuntimeError("ledger down")
        def _read_all(self): return []
        def query(self, **kw): return []

    svc = BillingService(ledger=FailingLedger())
    # publish should not silently swallow — should raise or at least not return normally without logging
    # Our contract: fail-closed -> raise
    try:
        svc.publish_factor(factor_id="fail_f", name="Fail", price=10, tenant="t1")
        raised = False
    except RuntimeError:
        raised = True
    except Exception:
        raised = True
    assert raised is True, "ledger failure must not be silently swallowed"


def test_billing_global_lock_exists():
    """Module-level globals must be guarded by threading.Lock."""
    import inspect
    import hero_quant.billing.service as mod
    assert hasattr(mod, "_GLOBAL_LOCK"), "missing _GLOBAL_LOCK"
    src = inspect.getsource(mod.BillingService.publish_factor)
    # publish_factor and purchase should use lock
    src2 = inspect.getsource(mod.BillingService.purchase)
    src_all = src + src2 + inspect.getsource(mod.BillingService.list_factors)
    assert "_GLOBAL_LOCK" in src_all or "with _GLOBAL_LOCK" in src_all


def test_billing_pg_noop_explicit_warning():
    """PG persistence no-op must log warning 'PG persistence not implemented, using emulated store' once."""
    import inspect
    from hero_quant.billing.service import BillingService
    import hero_quant.billing.service as mod
    src = inspect.getsource(mod.BillingService.__init__)
    mod_src = inspect.getsource(mod)
    assert "PG persistence not implemented" in mod_src, "warning string missing"
    # DDL should be gated or removed, not dead no-op
    # check _pg_publish_sync is not bare pass without warning
    pg_src = inspect.getsource(mod.BillingService._pg_publish_sync)
    assert "PG persistence not implemented" in pg_src or "emulated" in pg_src.lower() or "warning" in pg_src.lower()


def test_billing_attribution_dedup_single_source():
    """Attribution must use single source of truth and dedup by purchase_id; double-attribution not possible."""
    import inspect
    from hero_quant.billing.service import BillingService, _GLOBAL_PURCHASES, _GLOBAL_FACTORS
    dsn = "postgresql://postgres:postgres@localhost:5432/hero_quant_billing_dedup_test"
    _GLOBAL_FACTORS.pop(dsn, None)
    _GLOBAL_PURCHASES.pop(dsn, None)
    svc = BillingService(dsn=dsn)
    svc.publish_factor(factor_id="dedup_f", name="Dedup", price=10.0, tenant="prov")
    receipt = svc.purchase(factor_id="dedup_f", buyer_tenant="buyer_x", price=10.0)
    # attribution baseline
    attr1 = svc.attribution("dedup_f")
    assert attr1["purchases"] == 1
    # duplicate same purchase_id in global (simulate retry / merge bug)
    pid = receipt.get("purchase_id") or receipt.get("id")
    # if no purchase_id yet, dedup key will be missing -> test source check
    src = inspect.getsource(svc.attribution)
    assert "purchase_id" in src or "dedup" in src.lower() or "set(" in src, "attribution must dedup by explicit key"
    # ensure no len-based merge
    assert "len(self._purchases) > len(relevant)" not in src
    assert "p not in relevant" not in src
    # functional double-insert: insert duplicate receipt with same purchase_id
    if pid is not None:
        dup = receipt.copy()
        # duplicate in global store
        _GLOBAL_PURCHASES[dsn].append(dup)
        # also duplicate in instance
        svc._purchases.append(dup)
        attr2 = svc.attribution("dedup_f")
        assert attr2["purchases"] == 1, f"double-attribution must not occur, got {attr2}"
        assert attr2["revenue"] == 10.0
    # else source-level check suffices


def test_billing_rls_all_read_paths_filter_tenant():
    """ALL factor read paths must filter by tenant: tenant B cannot read tenant A's factor."""
    from hero_quant.billing.service import BillingService, _GLOBAL_FACTORS, _GLOBAL_PURCHASES
    dsn = "postgresql://postgres:postgres@localhost:5432/hero_quant_billing_rls_all"
    _GLOBAL_FACTORS.pop(dsn, None)
    _GLOBAL_PURCHASES.pop(dsn, None)
    svc = BillingService(dsn=dsn)
    svc.publish_factor(factor_id="rls_f", name="RLS", price=99.0, tenant="tenantA")
    # list_factors isolates
    assert len(svc.list_factors(tenant="tenantA")) == 1
    assert len(svc.list_factors(tenant="tenantB")) == 0
    # get_factor must also isolate when tenant supplied
    # new signature get_factor(factor_id, tenant=...)
    try:
        got_a = svc.get_factor("rls_f", tenant="tenantA")
    except TypeError:
        got_a = svc.get_factor("rls_f")  # fallback old signature
    try:
        got_b = svc.get_factor("rls_f", tenant="tenantB")
    except TypeError:
        # if old signature, then this test expects failure (fragile)
        got_b = None
        assert False, "get_factor must accept tenant param for RLS"
    assert got_a is not None, "tenantA should read own factor"
    assert got_b is None, "tenantB must NOT read tenantA's factor via get_factor"
    # generic: any read path should not leak
    if hasattr(svc, "get_factor"):
        import inspect
        src = inspect.getsource(svc.get_factor)
        assert "tenant" in src.lower()


def test_publish_factor_conflict_without_flag_raises():
    """publish_factor overwrites existing factor_id without validation → must raise unless allow_overwrite/upsert."""
    from hero_quant.billing.service import BillingService, _GLOBAL_FACTORS, _GLOBAL_PURCHASES
    import inspect

    # PG emulated mode
    dsn = "postgresql://postgres:postgres@localhost:5432/hero_quant_billing_conflict_test"
    _GLOBAL_FACTORS.pop(dsn, None)
    _GLOBAL_PURCHASES.pop(dsn, None)
    svc = BillingService(dsn=dsn)
    svc.publish_factor(factor_id="conflict_f", name="F", price=10.0, tenant="prov")
    # republish same id without flag must raise
    try:
        svc.publish_factor(factor_id="conflict_f", name="F2", price=20.0, tenant="prov")
        assert False, "silent overwrite not allowed"
    except ValueError as e:
        assert "already exists" in str(e).lower() or "conflict" in str(e).lower()
    # with allow_overwrite flag overwrites
    out = svc.publish_factor(factor_id="conflict_f", name="F2", price=20.0, tenant="prov", allow_overwrite=True)
    assert out["price"] == 20.0
    assert svc.get_factor("conflict_f")["price"] == 20.0
    # with upsert alias also overwrites
    out2 = svc.publish_factor(factor_id="conflict_f", name="F3", price=30.0, tenant="prov", upsert=True)
    assert out2["price"] == 30.0

    # memory mode also forbids silent overwrite
    svc2 = BillingService()
    svc2.publish_factor(factor_id="mem_conflict", name="M", price=1, tenant="t1")
    try:
        svc2.publish_factor(factor_id="mem_conflict", name="M2", price=2, tenant="t1")
        assert False, "memory silent overwrite not allowed"
    except ValueError:
        pass
    # with flag ok
    svc2.publish_factor(factor_id="mem_conflict", name="M2", price=2, tenant="t1", allow_overwrite=True)
    assert svc2.get_factor("mem_conflict")["price"] == 2

    # source-level check: must contain validation branch
    src = inspect.getsource(svc.publish_factor)
    assert "already exists" in src.lower() or "allow_overwrite" in src
    assert "upsert" in src.lower()

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
    # entries stored with tenant/price
    entries = ledger._read_all()
    assert any(e.get("tenant") == "tenant_a" for e in entries)
    assert any(e.get("price") == 99.5 for e in entries)


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
    text = p.read_text(encoding="utf-8")
    # replace first alice payload amount
    tampered = text.replace('"factor": "f1"', '"factor": "HACKED"', 1)
    p.write_text(tampered, encoding="utf-8")
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

    # RLS query API — must be simple where tenant=...
    assert hasattr(ledger, "query") or hasattr(ledger, "query_by_tenant") or hasattr(ledger, "list_records")
    # try canonical names
    if hasattr(ledger, "query"):
        r1 = ledger.query(tenant="t1")
        r2 = ledger.query(tenant="t2")
    elif hasattr(ledger, "query_by_tenant"):
        r1 = ledger.query_by_tenant("t1")
        r2 = ledger.query_by_tenant("t2")
    else:
        r1 = ledger.list_records(tenant="t1")
        r2 = ledger.list_records(tenant="t2")

    assert len(r1) == 2
    assert len(r2) == 1
    assert all(x["record"]["id"] != 2 for x in r1)
    assert r2[0]["record"]["id"] == 2


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
    assert receipt["tenant"] == "buyer_x" or receipt.get("buyer_tenant") == "buyer_x"
    # ledger isolated verification
    assert ledger.verify(tenant="buyer_x") is True
    # attribution closed loop: factor revenue aggregated
    attr = svc.attribution(factor_id="f1")
    assert attr["purchases"] >= 1
    assert attr["revenue"] >= 50.0
    # RLS: buyer_x purchases visible only to buyer_x, not to other tenant
    if hasattr(svc, "list_purchases"):
        assert len(svc.list_purchases(tenant="buyer_x")) == 1
        assert len(svc.list_purchases(tenant="other")) == 0
    # ledger RLS isolation also holds
    if hasattr(ledger, "query"):
        assert len(ledger.query(tenant="buyer_x")) == 1
        assert len(ledger.query(tenant="other")) == 0

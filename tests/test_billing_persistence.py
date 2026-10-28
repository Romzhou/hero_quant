"""Task8 TDD: billing PG+RLS persistence.

契约（S4 起）：
- 无 DSN / 无池 且未开 HERO_BILLING_ALLOW_MEMORY → fail-closed 抛 BillingUnavailable，不产出 purchase_id。
- 显式 ALLOW_MEMORY=1 → 内存降级，返回体必须带 degraded:true + reason="billing:memory"。
- 真 PG 语义（RLS 隔离 / 重启不丢）必须走真实池，无 PG 时 skip，不得用 ALLOW_MEMORY 蒙混。
"""
import os

import pytest

from conftest import pg_guard_reason, pg_test_dsn

_PG_GUARD_REASON = pg_guard_reason()
_pg_required = pytest.mark.skipif(_PG_GUARD_REASON is not None, reason=_PG_GUARD_REASON or "PG required")


@_pg_required
def test_billing_pg_rls_isolation():
    """RLS isolation (2 tenants, query with tenant filter) and tenant = string field — 真 PG 路径."""
    import uuid

    from hero_quant.billing.service import BillingService

    dsn = pg_test_dsn("billing_rls_isolation")
    svc = BillingService(dsn=dsn)
    assert svc._is_real_pg() is True, "PG guard 判定可用，但 BillingService 未建池"
    suffix = uuid.uuid4().hex[:8]
    alpha, beta = f"alpha_mom_{suffix}", f"beta_rev_{suffix}"
    try:
        svc.publish_factor(factor_id=alpha, name="Momentum", price=99.0, tenant="provider_a", description="desc")
        svc.publish_factor(factor_id=beta, name="Reversal", price=199.0, tenant="provider_b", description="desc2")
        f = svc.get_factor(alpha, tenant="provider_a")
        assert f is not None and isinstance(f["tenant"], str)
        fa = svc.list_factors(tenant="provider_a")
        fb = svc.list_factors(tenant="provider_b")
        assert any(x["factor_id"] == alpha for x in fa)
        assert any(x["factor_id"] == beta for x in fb)
        assert all(x["factor_id"] != beta for x in fa)
        assert all(x["factor_id"] != alpha for x in fb)
        assert len(svc.list_factors(tenant=f"other_{suffix}")) == 0
        buyer = f"buyer_x_{suffix}"
        svc.purchase(factor_id=alpha, buyer_tenant=buyer)
        assert len(svc.list_purchases(tenant=buyer)) == 1
        assert len(svc.list_purchases(tenant=f"other_{suffix}")) == 0
    finally:
        svc.close()


@_pg_required
def test_billing_restart_not_lost():
    """Restart not lost — new BillingService instance same DSN reads persisted PG rows."""
    import uuid

    from hero_quant.billing.service import BillingService

    dsn = pg_test_dsn("billing_restart")
    suffix = uuid.uuid4().hex[:8]
    fid = f"f_restart_{suffix}"
    buyer = f"buyer1_{suffix}"
    svc1 = BillingService(dsn=dsn)
    try:
        svc1.publish_factor(factor_id=fid, name="F Restart", price=50.0, tenant="prov")
        svc1.purchase(factor_id=fid, buyer_tenant=buyer)
    finally:
        svc1.close()
    svc2 = BillingService(dsn=dsn)
    try:
        got = svc2.get_factor(fid, tenant="prov")
        assert got is not None and float(got["price"]) == 50.0
        assert len(svc2.list_purchases(tenant=buyer)) == 1
        attr = svc2.attribution(fid)
        assert attr["purchases"] == 1
        assert float(attr["revenue"]) == 50.0
    finally:
        svc2.close()


def test_billing_memory_fallback_when_pg_dsn_not_set(monkeypatch):
    """无 DSN 默认 fail-closed；显式 ALLOW_MEMORY=1 才允许内存降级并打 degraded。"""
    from hero_quant.billing.service import BillingService, BillingUnavailable

    for key in ("HERO_BILLING_DSN", "HERO_PG_DSN", "HERO_CHECKPOINT_DSN"):
        monkeypatch.setenv(key, "")
    monkeypatch.delenv("HERO_BILLING_ALLOW_MEMORY", raising=False)
    from hero_quant.config.settings import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]

    svc = BillingService(dsn=None)
    assert svc._is_pg_mode() is False
    with pytest.raises(BillingUnavailable) as ei:
        svc.publish_factor(factor_id="mem_fail", name="Mem", price=10.0, tenant="t1")
    assert ei.value.reason == "billing:no_dsn"
    with pytest.raises(BillingUnavailable) as ei2:
        svc.purchase(factor_id="mem_fail", buyer_tenant="b1")
    assert ei2.value.reason == "billing:no_dsn"
    assert svc.list_factors(tenant="t1") == []

    monkeypatch.setenv("HERO_BILLING_ALLOW_MEMORY", "1")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    svc2 = BillingService(dsn="")
    assert svc2._is_pg_mode() is False
    out = svc2.publish_factor(factor_id="mem_f", name="Mem", price=10.0, tenant="t1")
    assert out["degraded"] is True and out["reason"] == "billing:memory"
    assert len(svc2.list_factors(tenant="t1")) == 1
    rc = svc2.purchase(factor_id="mem_f", buyer_tenant="b1")
    assert rc["degraded"] is True and rc["reason"] == "billing:memory"
    assert rc.get("purchase_id")


def test_billing_tenant_string_field(billing_memory):
    from hero_quant.billing.service import BillingService

    svc = BillingService(dsn="memory://billing_tenant_test")
    out = svc.publish_factor(factor_id="fid", name="F", price=10, tenant="tenant_str")
    assert out["degraded"] is True and out["reason"] == "billing:memory"
    f = svc.get_factor("fid")
    assert isinstance(f["tenant"], str)
    p = os.path.join("migrations", "002_billing_rls.sql")
    if os.path.exists(p):
        txt = open(p, encoding="utf-8").read()
        assert "tenant" in txt

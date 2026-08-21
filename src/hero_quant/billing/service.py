"""Minimal factor marketplace billing — factor as asset, billing → attribution closed loop."""
from __future__ import annotations

from typing import Dict, List, Optional


class BillingService:
    """Factor marketplace with multi-tenant RLS isolation.

    Keep RLS isolation simple: where tenant == ...
    No over-engineering: in-memory factors + purchases, optional ledger sync.
    """

    def __init__(self, ledger=None):
        self.ledger = ledger
        self._factors: Dict[str, dict] = {}
        self._purchases: List[dict] = []

    def publish_factor(
        self,
        factor_id: str,
        name: str,
        price: float,
        tenant: str = "default",
        description: str = "",
    ) -> dict:
        factor = {
            "factor_id": factor_id,
            "name": name,
            "price": float(price),
            "tenant": tenant,
            "description": description,
        }
        self._factors[factor_id] = factor
        # optional ledger append for provenance (provider tenant)
        if self.ledger is not None:
            try:
                self.ledger.append(
                    {"action": "publish_factor", "factor_id": factor_id, "name": name},
                    tenant=tenant,
                    price=float(price),
                )
            except Exception:
                pass
        return factor

    def list_factors(self, tenant: str | None = None) -> List[dict]:
        if tenant is None:
            return list(self._factors.values())
        return [f for f in self._factors.values() if f.get("tenant") == tenant]

    def get_factor(self, factor_id: str) -> Optional[dict]:
        return self._factors.get(factor_id)

    def purchase(
        self,
        factor_id: str,
        buyer_tenant: str,
        price: float | None = None,
    ) -> dict:
        factor = self._factors.get(factor_id)
        if factor is None:
            raise ValueError(f"factor not found: {factor_id}")
        use_price = float(price) if price is not None else float(factor["price"])
        receipt = {
            "factor_id": factor_id,
            "buyer_tenant": buyer_tenant,
            "tenant": buyer_tenant,
            "price": use_price,
            "action": "purchase_factor",
        }
        self._purchases.append(receipt)
        if self.ledger is not None:
            try:
                self.ledger.append(
                    {"action": "purchase_factor", "factor_id": factor_id},
                    tenant=buyer_tenant,
                    price=use_price,
                )
            except Exception:
                pass
        return receipt

    def attribution(self, factor_id: str) -> dict:
        """Attribution closed loop: purchases + revenue for factor."""
        relevant = [p for p in self._purchases if p.get("factor_id") == factor_id]
        # also count from ledger if available and purchases empty fallback
        if self.ledger is not None:
            try:
                _entries = self.ledger.query(tenant=None) if hasattr(self.ledger, "_read_all") else []  # placeholder
            except Exception:
                _entries = []  # noqa: F841 - placeholder for future use
            # ledger-based counting as fallback: scan ledger for purchase_factor
            # we already have _purchases, but if ledger has extra entries not in memory (e.g., restarted service),
            # include them
            try:
                all_entries = self.ledger._read_all()  # type: ignore
                ledger_purchases = [
                    e
                    for e in all_entries
                    if e.get("record", {}).get("action") == "purchase_factor"
                    and e.get("record", {}).get("factor_id") == factor_id
                ]
                # if ledger has more than memory, merge (dedup by count)
                if len(ledger_purchases) > len(relevant):
                    # estimate revenue from ledger prices
                    rev = sum(float(e.get("price", 0) or 0) for e in ledger_purchases)
                    return {
                        "factor_id": factor_id,
                        "purchases": len(ledger_purchases),
                        "revenue": rev,
                    }
            except Exception:
                pass
        revenue = sum(float(p.get("price", 0)) for p in relevant)
        return {"factor_id": factor_id, "purchases": len(relevant), "revenue": revenue}

    def list_purchases(self, tenant: str) -> List[dict]:
        """RLS: where tenant == buyer_tenant."""
        return [p for p in self._purchases if p.get("buyer_tenant") == tenant or p.get("tenant") == tenant]

    # alias for compatibility
    def list_purchases_by_tenant(self, tenant: str) -> List[dict]:
        return self.list_purchases(tenant)

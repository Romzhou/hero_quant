"""因子市场计费 —— 因子即资产，计费到归因闭环。

职责：提供因子发布、购买与归因统计；架构位置：billing 域，依赖可选 ledger 做来源追溯。
设计决策：以 tenant 为隔离维度，内存存储因子与购买记录，ledger 仅作追加与溯源的外部同步。
"""
from __future__ import annotations

from typing import Dict, List, Optional


class BillingService:
    """因子市场服务，多租户行级隔离；内存态因子与购买记录，可选 ledger 同步溯源。"""

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
        """发布因子，记录归属租户与定价。"""
        factor = {
            "factor_id": factor_id,
            "name": name,
            "price": float(price),
            "tenant": tenant,
            "description": description,
        }
        self._factors[factor_id] = factor
        # 同步到 ledger 以保留发布溯源（按提供方 tenant）
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
        """按租户列出因子，未指定则返回全部。"""
        if tenant is None:
            return list(self._factors.values())
        return [f for f in self._factors.values() if f.get("tenant") == tenant]

    def get_factor(self, factor_id: str) -> Optional[dict]:
        """按 ID 获取因子。"""
        return self._factors.get(factor_id)

    def purchase(
        self,
        factor_id: str,
        buyer_tenant: str,
        price: float | None = None,
    ) -> dict:
        """购买因子，生成购买收据并可选同步 ledger。"""
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
        """归因闭环：统计指定因子的购买次数与总收入。"""
        relevant = [p for p in self._purchases if p.get("factor_id") == factor_id]
        # 若 ledger 可用，以其持久化记录作为补充，避免内存重启后统计丢失
        if self.ledger is not None:
            try:
                _entries = self.ledger.query(tenant=None) if hasattr(self.ledger, "_read_all") else []  # placeholder
            except Exception:
                _entries = []  # noqa: F841 - 占位，未来扩展查询
            # 若内存与 ledger 不一致（例如服务重启后），以 ledger 为准补齐
            # 已有内存购买记录，仅当 ledger 更多时才合并
            try:
                all_entries = self.ledger._read_all()  # type: ignore
                ledger_purchases = [
                    e
                    for e in all_entries
                    if e.get("record", {}).get("action") == "purchase_factor"
                    and e.get("record", {}).get("factor_id") == factor_id
                ]
                # 仅当 ledger 记录更多时合并，按计数去重视为增量
                if len(ledger_purchases) > len(relevant):
                    # 按 ledger 中的价格汇总收入
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
        """按租户列出购买记录（行级隔离：buyer_tenant == tenant）。"""
        return [p for p in self._purchases if p.get("buyer_tenant") == tenant or p.get("tenant") == tenant]

    # 兼容别名
    def list_purchases_by_tenant(self, tenant: str) -> List[dict]:
        """按租户列出购买记录的兼容别名。"""
        return self.list_purchases(tenant)

"""billing 包 —— 因子市场与多租户计费。

职责：暴露 BillingService；架构位置：业务域层，按 tenant 隔离发布与购买。
设计决策：因子即资产，计费与归因闭环，ledger 可选用于溯源。
"""

from hero_quant.billing.service import BillingService

__all__ = ["BillingService"]

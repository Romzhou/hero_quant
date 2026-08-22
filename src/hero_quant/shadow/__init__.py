"""影子账户包入口 — 汇集台账、风控引擎与归因能力。"""

from .service import (
    ATTRIBUTION_CATEGORIES,
    ATTRIBUTION_CN,
    DEFAULT_RULES,
    RiskEngine,
    ShadowAccount,
    ShadowJournal,
    ShadowRule,
)

__all__ = [
    "ShadowRule",
    "ShadowJournal",
    "ShadowAccount",
    "RiskEngine",
    "DEFAULT_RULES",
    "ATTRIBUTION_CATEGORIES",
    "ATTRIBUTION_CN",
]

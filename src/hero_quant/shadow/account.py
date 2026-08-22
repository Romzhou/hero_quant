"""兼容别名——重导出 service 的 Shadow 2.0 能力，保持历史导入路径可用。"""
from .service import *  # noqa: F401,F403
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

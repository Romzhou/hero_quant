"""Compatibility alias for service.py — exposes same Shadow 2.0 API."""
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

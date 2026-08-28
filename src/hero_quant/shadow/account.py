"""兼容别名——重导出 service 的 Shadow 2.0 能力，保持历史导入路径可用。"""

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

# 保持与 service 同步：__all__ 需为 service 公开符号子集，测试中校验
# assert set(__all__) <= set(dir(__import__("hero_quant.shadow.service", fromlist=["*"]))); 避免 from .service import * 污染
# 当前显式导入已避免 logger 等非预期符号泄漏

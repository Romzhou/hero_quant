"""skills 包 —— 两阶段技能披露。

职责：暴露 SkillsLoader；架构位置：skills 域，衔接上下文注入与工具按需加载。
设计决策：首阶段仅摘要（<500 字符），次阶段按名取全文；多根覆盖与 mtime 热失效保证新鲜度。
"""

from .loader import SkillsLoader

__all__ = ["SkillsLoader"]

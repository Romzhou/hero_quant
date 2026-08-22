"""config 包 —— 统一配置入口。

职责：暴露 Settings 与截断常量；架构位置：最底层配置层。
设计约定：环境变量映射 HERO_* 仅在 settings.Settings 中解析，limits 提供全局字符预算。
"""

from hero_quant.config.settings import Settings

__all__ = ["Settings"]

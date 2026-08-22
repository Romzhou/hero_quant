"""core 包 —— 作用域与多租户隔离基座。

职责：提供 Scope 层级与 ScopedLayers 合并能力；架构位置：core 层供上层按 tenant scope 隔离。
"""

from .scope import Scope, ScopedLayers, create_scope, link_scope_parent

__all__ = ["Scope", "ScopedLayers", "create_scope", "link_scope_parent"]

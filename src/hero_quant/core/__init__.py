"""hero_quant.core — scope hierarchy for multi-tenant preset isolation."""

from .scope import Scope, ScopedLayers, create_scope, link_scope_parent

__all__ = ["Scope", "ScopedLayers", "create_scope", "link_scope_parent"]

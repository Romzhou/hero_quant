"""Scope hierarchy + layered registry seam.

Semantics:
- Scope is a node in a parent chain (multi-tenant isolation).
- ScopedLayers stores per-scope key-values; merge walks root→leaf with nearest-wins.
- Matches dsh / Direnv-style chain_layers semantics.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional


class Scope:
    """A named scope node that may link to a parent for hierarchical merging."""

    def __init__(self, key: str, parent: Optional["Scope"] = None):
        self.key: str = key
        # alias for ergonomics
        self.name: str = key
        self.parent: Optional["Scope"] = parent

    def __repr__(self) -> str:
        parent_key = getattr(self.parent, "key", None)
        if parent_key:
            return f"Scope(key={self.key!r}, parent={parent_key!r})"
        return f"Scope(key={self.key!r})"


def create_scope(key: str, parent: Optional[Scope] = None) -> Scope:
    """Create a new scope optionally linked to a parent."""
    return Scope(key, parent=parent)


def link_scope_parent(child: Scope, parent: Scope) -> Scope:
    """Link child scope to parent (mutates child) and return child."""
    if child is None or parent is None:
        raise ValueError("child and parent must be non-None Scope")
    child.parent = parent
    return child


class ScopedLayers:
    """Per-scope layered storage with chain merge (child overrides parent)."""

    def __init__(self) -> None:
        # Use dict with Scope as key (identity hash). Values are shallow dicts.
        self._store: Dict[Scope, Dict[str, Any]] = {}

    def set(self, scope: Scope, vals: Dict[str, Any]) -> None:
        """Set / upsert values for a scope (shallow merge on same scope)."""
        if scope is None:
            raise ValueError("scope must not be None")
        if not isinstance(vals, dict):
            raise TypeError("vals must be a dict")
        existing = self._store.get(scope)
        if existing is None:
            # copy to avoid external mutation
            self._store[scope] = dict(vals)
        else:
            existing.update(vals)

    def chain_layers(self, scope: Scope) -> List[Scope]:
        """Return chain from root → leaf for given scope (inclusive)."""
        chain: List[Scope] = []
        cur: Optional[Scope] = scope
        # guard against cycles
        seen: set[int] = set()
        while cur is not None:
            cid = id(cur)
            if cid in seen:
                break
            seen.add(cid)
            chain.append(cur)
            cur = getattr(cur, "parent", None)
        chain.reverse()
        return chain

    def merge(self, scope: Scope) -> Dict[str, Any]:
        """Merge all layers along the scope chain; nearest (leaf) wins."""
        merged: Dict[str, Any] = {}
        for layer in self.chain_layers(scope):
            vals = self._store.get(layer)
            if vals:
                merged.update(vals)
        return merged

    # Optional helpers kept minimal for future seam usage
    def get(self, scope: Scope, key: str, default: Any = None) -> Any:
        """Get merged value for key on scope chain."""
        return self.merge(scope).get(key, default)

    def clear(self, scope: Scope) -> None:
        """Clear all values for a scope."""
        self._store.pop(scope, None)

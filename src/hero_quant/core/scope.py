"""作用域与分层注册 —— 多租户隔离基座。

职责：提供 Scope 层级与 ScopedLayers 合并能力，支撑 tenant scope 级别的配置隔离。
架构位置：core 层，被 preset / billing / skills 等上层按 scope 维度复用。
设计决策：Scope 为父链节点，ScopedLayers 按 root→leaf 合并且子级覆盖父级；链遍历带环检测，避免异常引用导致死循环。
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional
import logging

logger = logging.getLogger(__name__)


class Scope:
    """作用域节点，key 为标识，parent 指向父级形成链路；name 为 key 的别名便于可读性。"""

    def __init__(self, key: str, parent: Optional["Scope"] = None):
        self.key: str = key
        # 别名便于外部以 scope.name 访问
        self.name: str = key
        self.parent: Optional["Scope"] = parent

    def __repr__(self) -> str:
        parent_key = getattr(self.parent, "key", None)
        if parent_key:
            return f"Scope(key={self.key!r}, parent={parent_key!r})"
        return f"Scope(key={self.key!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Scope):
            return False
        if self.key != other.key:
            return False
        # recursive parent equality on value fields
        if self.parent is None and other.parent is None:
            return True
        if self.parent is None or other.parent is None:
            return False
        return self.parent == other.parent

    def __hash__(self) -> int:
        # hash on value fields (key + parent) — logical duplicates deduplicate
        # guard recursion if cycle exists (should already be rejected)
        try:
            return hash((self.key, self.parent))
        except RecursionError:
            logger.error("cycle detected during hash for scope %r", self.key)
            return hash(self.key) ^ id(self.parent)


def create_scope(key: str, parent: Optional[Scope] = None) -> Scope:
    """创建作用域，可选关联父级。"""
    return Scope(key, parent=parent)


def link_scope_parent(child: Scope, parent: Scope) -> Scope:
    """为子作用域绑定父级（原地修改）并返回子级。"""
    if child is None or parent is None:
        raise ValueError("child and parent must be non-None Scope")
    # cycle detection: walk parent chain to ensure child not ancestor
    cur: Optional[Scope] = parent
    seen: set[int] = set()
    while cur is not None:
        if cur is child or cur == child:
            logger.error("cycle detected in link_scope_parent: child=%r parent=%r", getattr(child, "key", None), getattr(parent, "key", None))
            raise ValueError(f"cycle detected: linking {child.key!r} under {parent.key!r} creates cycle")
        cid = id(cur)
        if cid in seen:
            logger.error("cycle detected traversing parent chain for %r", getattr(parent, "key", None))
            raise ValueError("cycle detected in parent chain")
        seen.add(cid)
        cur = getattr(cur, "parent", None)
    child.parent = parent
    return child


class ScopedLayers:
    """按作用域分层的键值存储，合并时子级覆盖父级。"""

    def __init__(self) -> None:
        # 以 Scope 值语义为键（已实现 __hash__/__eq__ 基于 key+parent），逻辑重复去重
        self._store: Dict[Scope, Dict[str, Any]] = {}

    def set(self, scope: Scope, vals: Dict[str, Any]) -> None:
        """为指定作用域写入/合并键值（同作用域内浅合并，深拷贝值以隔离租户嵌套可变状态）。"""
        if scope is None:
            raise ValueError("scope must not be None")
        if not isinstance(vals, dict):
            raise TypeError("vals must be a dict")
        # deep copy values to prevent cross-tenant nested mutation leakage
        deep_vals = copy.deepcopy(vals)
        existing = self._store.get(scope)
        if existing is None:
            # 拷贝避免外部后续修改影响存储（含嵌套可变对象）
            self._store[scope] = deep_vals
        else:
            existing.update(deep_vals)

    def chain_layers(self, scope: Scope) -> List[Scope]:
        """返回从根到叶的链路（含自身），用于按序合并。"""
        chain: List[Scope] = []
        cur: Optional[Scope] = scope
        # 环检测：raise on cycle to surface misconfiguration (previously silent truncation)
        seen: set[int] = set()
        seen_eq: set[Scope] = set()
        while cur is not None:
            cid = id(cur)
            if cid in seen:
                logger.error("cycle detected in chain_layers for scope %r", getattr(scope, "key", None))
                raise ValueError(f"cycle detected in scope chain at {cur.key!r}")
            # also detect logical cycle via == (covers identity-keyed duplicates)
            if cur in seen_eq:
                logger.error("cycle detected (logical) in chain_layers for scope %r", getattr(scope, "key", None))
                raise ValueError(f"cycle detected (logical) in scope chain at {cur.key!r}")
            seen.add(cid)
            seen_eq.add(cur)
            chain.append(cur)
            cur = getattr(cur, "parent", None)
        chain.reverse()
        return chain

    def merge(self, scope: Scope) -> Dict[str, Any]:
        """合并链路上所有层，近端（子级）覆盖远端；深拷贝嵌套值以隔离租户。"""
        merged: Dict[str, Any] = {}
        for layer in self.chain_layers(scope):
            vals = self._store.get(layer)
            if vals:
                # deep copy each value to avoid cross-tenant nested mutation leakage via parent layer
                for k, v in vals.items():
                    merged[k] = copy.deepcopy(v)
        return merged

    # 便于上层直接取合并后的单键值
    def get(self, scope: Scope, key: str, default: Any = None) -> Any:
        """按链路合并后取值。"""
        return self.merge(scope).get(key, default)

    def clear(self, scope: Scope) -> None:
        """清空指定作用域的全部键值。"""
        self._store.pop(scope, None)

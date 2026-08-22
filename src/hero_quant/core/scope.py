"""作用域与分层注册 —— 多租户隔离基座。

职责：提供 Scope 层级与 ScopedLayers 合并能力，支撑 tenant scope 级别的配置隔离。
架构位置：core 层，被 preset / billing / skills 等上层按 scope 维度复用。
设计决策：Scope 为父链节点，ScopedLayers 按 root→leaf 合并且子级覆盖父级；链遍历带环检测，避免异常引用导致死循环。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


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


def create_scope(key: str, parent: Optional[Scope] = None) -> Scope:
    """创建作用域，可选关联父级。"""
    return Scope(key, parent=parent)


def link_scope_parent(child: Scope, parent: Scope) -> Scope:
    """为子作用域绑定父级（原地修改）并返回子级。"""
    if child is None or parent is None:
        raise ValueError("child and parent must be non-None Scope")
    child.parent = parent
    return child


class ScopedLayers:
    """按作用域分层的键值存储，合并时子级覆盖父级。"""

    def __init__(self) -> None:
        # 以 Scope 实例为键（依赖对象身份），值为浅拷贝的字典
        self._store: Dict[Scope, Dict[str, Any]] = {}

    def set(self, scope: Scope, vals: Dict[str, Any]) -> None:
        """为指定作用域写入/合并键值（同作用域内浅合并）。"""
        if scope is None:
            raise ValueError("scope must not be None")
        if not isinstance(vals, dict):
            raise TypeError("vals must be a dict")
        existing = self._store.get(scope)
        if existing is None:
            # 拷贝避免外部后续修改影响存储
            self._store[scope] = dict(vals)
        else:
            existing.update(vals)

    def chain_layers(self, scope: Scope) -> List[Scope]:
        """返回从根到叶的链路（含自身），用于按序合并。"""
        chain: List[Scope] = []
        cur: Optional[Scope] = scope
        # 环检测：避免父链误配置导致无限循环
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
        """合并链路上所有层，近端（子级）覆盖远端。"""
        merged: Dict[str, Any] = {}
        for layer in self.chain_layers(scope):
            vals = self._store.get(layer)
            if vals:
                merged.update(vals)
        return merged

    # 便于上层直接取合并后的单键值
    def get(self, scope: Scope, key: str, default: Any = None) -> Any:
        """按链路合并后取值。"""
        return self.merge(scope).get(key, default)

    def clear(self, scope: Scope) -> None:
        """清空指定作用域的全部键值。"""
        self._store.pop(scope, None)

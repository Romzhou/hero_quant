# tests/test_scope.py
def test_scope_chain_layers():
    from hero_quant.core.scope import ScopedLayers, create_scope

    parent = create_scope("research")
    child = create_scope("factor", parent=parent)
    layers = ScopedLayers()
    layers.set(parent, {"tool": "tencent"})
    layers.set(child, {"tool": "yahoo"})
    assert layers.merge(child)["tool"] == "yahoo"
    assert layers.merge(parent)["tool"] == "tencent"


import pytest

def test_scope_cycle_raises():
    from hero_quant.core.scope import ScopedLayers, create_scope, link_scope_parent
    a = create_scope("a")
    b = create_scope("b", parent=a)
    with pytest.raises((ValueError, RuntimeError)):
        link_scope_parent(a, b)
        layers = ScopedLayers()
        layers.chain_layers(a)

def test_scope_chain_cycle_detection_raises():
    from hero_quant.core.scope import Scope, ScopedLayers
    a = Scope("a")
    b = Scope("b", parent=a)
    a.parent = b
    layers = ScopedLayers()
    with pytest.raises((ValueError, RuntimeError)):
        layers.chain_layers(a)

def test_scope_hash_eq_deduplication():
    from hero_quant.core.scope import Scope, ScopedLayers
    parent = Scope("research")
    child1 = Scope("factor", parent=parent)
    child2 = Scope("factor", parent=Scope("research"))
    assert child1 == child2
    assert hash(child1) == hash(child2)
    layers = ScopedLayers()
    layers.set(child1, {"tool": "tencent"})
    layers.set(child2, {"tool": "yahoo"})
    assert len(layers._store) == 1
    assert layers.get(child1, "tool") == "yahoo"
    assert layers.get(child2, "tool") == "yahoo"

def test_scope_hash_distinguishes_different_parent():
    from hero_quant.core.scope import Scope
    p1 = Scope("p1")
    p2 = Scope("p2")
    c1 = Scope("child", parent=p1)
    c2 = Scope("child", parent=p2)
    assert c1 != c2
    assert hash(c1) != hash(c2)

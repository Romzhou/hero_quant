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

def test_scope_shallow_copy_isolation():
    """P2-1: ScopedLayers.set must deepcopy nested mutable state; tenant A mutation must not affect tenant B."""
    from hero_quant.core.scope import ScopedLayers, Scope
    parent = Scope("global")
    child_a = Scope("tenant_a", parent=parent)
    child_b = Scope("tenant_b", parent=parent)
    layers = ScopedLayers()
    nested = {"cfg": {"x": 1, "inner": [1, 2]}}
    layers.set(parent, nested)
    # mutate original after set should not affect store
    nested["cfg"]["x"] = 999
    nested["cfg"]["inner"].append(3)
    m_parent = layers.merge(parent)
    assert m_parent["cfg"]["x"] == 1
    assert m_parent["cfg"]["inner"] == [1, 2]
    # tenant A mutates merged nested dict
    m_a = layers.merge(child_a)
    m_a["cfg"]["x"] = 999
    m_a["cfg"]["inner"].append(9)
    # tenant B must be unaffected
    m_b = layers.merge(child_b)
    assert m_b["cfg"]["x"] == 1, "tenant B affected by tenant A nested mutation"
    assert m_b["cfg"]["inner"] == [1, 2]
    # also verify store deep isolation: parent store still intact
    assert layers.merge(parent)["cfg"]["x"] == 1

def test_scope_merge_deepcopy_isolation():
    """Additional: merge result mutation does not leak into store."""
    from hero_quant.core.scope import ScopedLayers, Scope
    s = Scope("s")
    layers = ScopedLayers()
    layers.set(s, {"d": {"k": [1]}})
    m1 = layers.merge(s)
    m1["d"]["k"].append(2)
    m2 = layers.merge(s)
    assert m2["d"]["k"] == [1]

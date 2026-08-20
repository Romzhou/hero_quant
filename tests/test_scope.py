# tests/test_scope.py
def test_scope_chain_layers():
    from hero_quant.core.scope import Scope, ScopedLayers, create_scope, link_scope_parent
    parent = create_scope("research")
    child = create_scope("factor", parent=parent)
    layers = ScopedLayers(); layers.set(parent, {"tool": "tencent"}); layers.set(child, {"tool": "yahoo"})
    assert layers.merge(child)["tool"] == "yahoo"
    assert layers.merge(parent)["tool"] == "tencent"

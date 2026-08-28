import pathlib
import pytest


def test_route_entry_rejects_traversal(tmp_path):
    from hero_quant.memory.hierarchy import MemoryHierarchy

    h = MemoryHierarchy(tmp_path)
    with pytest.raises(ValueError):
        h.route_entry("research", "../evil.md")
    with pytest.raises(ValueError):
        h.route_entry("research", "/etc/passwd")
    # also test backslash and absolute windows path
    with pytest.raises(ValueError):
        h.route_entry("user", "a\\b.md")
    with pytest.raises(ValueError):
        h.route_entry("user", "../a.md")


def test_yaml_safe_dump_used():
    src = pathlib.Path("src/hero_quant/memory/hierarchy.py").read_text(encoding="utf-8")
    assert "yaml.safe_dump" in src or "safe_dump" in src
    # also ensure atomic replace via tmp
    assert "tmp" in src.lower() and "replace" in src.lower()

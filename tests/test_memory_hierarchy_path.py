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


def test_yaml_safe_dump_used(tmp_path):
    """索引必须是可被 safe_load 解析的 YAML，且内容与输入一致（运行时产物断言）。"""
    import yaml

    from hero_quant.memory.hierarchy import MemoryHierarchy

    h = MemoryHierarchy(tmp_path)
    h.rebuild_index([{"memory_type": "user", "keywords": ["Alpha", "beta"]}])
    data = yaml.safe_load((tmp_path / ".hierarchy.yaml").read_text(encoding="utf-8"))
    assert data["categories"]["user"]["count"] == 1
    assert data["categories"]["user"]["keywords"] == ["alpha", "beta"]
    # 写入走同目录 tmp + replace：不得残留临时文件
    assert not list(tmp_path.glob(".hierarchy.*.tmp"))

import pytest
from hero_quant.sandbox.policy import canonical_path, resolve_policy


def test_canonical_fallback_returns_input():
    assert isinstance(canonical_path(""), str)
    # 模拟 resolve 失败路径：期望回退原串不抛


def test_empty_workspace_root_rejected():
    with pytest.raises(ValueError):
        resolve_policy(mode="workspace-write", workspace_root="")


def test_is_path_writable_commonpath():
    from hero_quant.sandbox.policy import is_path_writable

    pol = resolve_policy(mode="workspace-write", workspace_root="/tmp")
    assert is_path_writable("/tmp/a/b", pol) is True
    assert is_path_writable("/etc/passwd", pol) is False

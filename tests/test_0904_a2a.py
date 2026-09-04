"""Lane A2a TDD — 凭证管理 7 条，每条一个 failing test（先红后绿）。

覆盖 scan_0904_g3_domain.log 中 security/credentials.py 7 节（G3 #81–87）：
0600 PermissionError 被 OSError 吞成 ValueError（critical）、write/fsync 失败 tmp orphan、
目录权限失败吞错、死 _check_0600、默认值过严误伤合法 secret、明文文件探测脆弱、OSError 重包 PermissionError。
契约：fail-closed；中文注释；窄化捕获 + logger（本模块用 warnings/raise，无 structlog，不引入新依赖）。
只动 src/hero_quant/security/credentials.py。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_a01_perm_error_not_downgraded(tmp_path):
    """critical：非 0600 文件必须抛 PermissionError，不可被 except OSError 吞成 ValueError。"""
    from hero_quant.security import credentials as cm

    p = tmp_path / "cred"
    p.write_text("secret", encoding="utf-8")
    os.chmod(p, 0o644)
    with pytest.raises(PermissionError):
        cm._read_credential_file(p)


def test_a02_write_failure_cleans_tmp(tmp_path):
    """write/fsync 失败不得残留 tmp 文件。"""
    from hero_quant.security import credentials as cm

    d = tmp_path / "creds"
    d.mkdir()
    target = d / "c.txt"
    leftovers_before = set(d.iterdir())
    with pytest.raises(Exception):
        cm.write_credential_file(target, "x" * 10**7 + "\ud800")
    leftovers_after = {x for x in d.iterdir() if x not in leftovers_before and x.name.startswith("c.txt.tmp.")}
    assert not leftovers_after, f"tmp orphan: {leftovers_after}"


def test_a03_mkdir_failure_is_loud(tmp_path, monkeypatch):
    """凭证目录创建/chmod 失败必须抛，不吞。"""
    from hero_quant.security import credentials as cm

    def _boom(*a, **k):
        raise OSError("mock mkdir boom")

    monkeypatch.setattr(Path, "mkdir", _boom)
    with pytest.raises(Exception):
        cm.write_credential_file(tmp_path / "nope" / "c.txt", "s")


def test_a04_check_0600_wired_or_removed():
    """死 _check_0600：要么被 _read_credential_file 复用，要么删除。"""
    import inspect

    from hero_quant.security import credentials as cm

    src = inspect.getsource(cm._read_credential_file)
    uses_helper = "_check_0600" in src or "_check_fd_0600" in src
    has_def = hasattr(cm, "_check_0600")
    assert uses_helper or not has_def, "死代码：_check_0600 未被调用也未删除"


def test_a05_slash_default_allowed():
    """含 / 的合法默认值（如 base64）不得被拒；路径穿越仍拒绝。"""
    from hero_quant.security import credentials as cm

    assert cm.resolve("${NO_SUCH_A2A_VAR_xyz:-abc/def+ghi=}") == "abc/def+ghi="
    with pytest.raises(ValueError):
        cm.resolve("${NO_SUCH_A2A_VAR_xyz:-../../etc/passwd}")


def test_a06_plain_value_no_probing():
    """无模式纯值不得做文件系统探测（超长/含换行直接回落原值）。"""
    from hero_quant.security import credentials as cm

    weird = "not-a-path\nwith-newline:" + "x" * 600
    assert cm.resolve(weird) == weird


def test_a07_perm_error_not_rewrapped(tmp_path, monkeypatch):
    """symlink 逃逸的 PermissionError 不得被外层 except OSError 重包（信息保留）。

    Windows 无提权时真 symlink 建不了，用 monkeypatch 模拟越界 symlink 走同样分支。
    """
    from hero_quant.security import credentials as cm

    link = tmp_path / "sub" / "link"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.write_text("s", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    jail = tmp_path / "jail"
    jail.mkdir(exist_ok=True)

    def _fake_resolve(self, strict=False):
        if self == link:
            return outside
        if self == link.parent:
            return jail
        return self

    monkeypatch.setattr(Path, "is_symlink", lambda self: True if self == link else False)
    monkeypatch.setattr(Path, "resolve", _fake_resolve)
    try:
        cm._read_credential_file(link)
    except PermissionError as e:
        assert "outside allowed dir" in str(e)
    else:
        raise AssertionError("应抛 PermissionError")

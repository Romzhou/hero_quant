import pytest
from hero_quant.sandbox.base import DockerBackend, LocalShellBackend


def test_str_cmd_rejected():
    b = LocalShellBackend(policy={"mode": "workspace-write", "workspaceRoot": "/tmp"})
    with pytest.raises(ValueError):
        b.execute("echo hi; id")


def test_workspace_symlink_rejected(tmp_path):
    import os
    from pathlib import Path
    from hero_quant.sandbox.base import is_path_writable
    # symlink escape: link inside tmp pointing outside should be rejected by commonpath guard
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to("/etc")
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported")
    pol = {"writableRoots": [str(target)], "mode": "workspace-write"}
    # linked path outside writable root should be non-writable after resolve
    assert is_path_writable(str(link / "passwd"), pol) is False


def test_docker_mount_colon_rejected():
    p = {"mode": "workspace-write", "workspaceRoot": "/tmp:evil"}
    with pytest.raises((ValueError, Exception)):
        DockerBackend(policy=p).confine(["echo", "hi"], p)

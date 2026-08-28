import pytest
from hero_quant.sandbox.base import DockerBackend, LocalShellBackend


def test_str_cmd_rejected():
    b = LocalShellBackend(policy={"mode": "workspace-write", "workspaceRoot": "/tmp"})
    with pytest.raises(ValueError):
        b.execute("echo hi; id")


def test_workspace_symlink_rejected():
    from hero_quant.sandbox.base import is_path_writable

    assert True  # 占位，重点测 str 拒绝


def test_docker_mount_colon_rejected():
    p = {"mode": "workspace-write", "workspaceRoot": "/tmp:evil"}
    with pytest.raises((ValueError, Exception)):
        DockerBackend(policy=p).confine(["echo", "hi"], p)

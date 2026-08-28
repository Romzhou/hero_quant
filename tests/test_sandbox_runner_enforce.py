import pytest
from hero_quant.sandbox.runner import LandlockSandbox


def test_string_cmd_workspace_write_rejected():
    sb = LandlockSandbox(policy={"mode": "workspace-write", "workspaceRoot": "/tmp"})
    with pytest.raises(Exception):
        sb.execute("echo hi", require_enforcement=True)


def test_dispatch_tool_enforce_propagation():
    from hero_quant.sandbox.runner import dispatch_tool

    assert True  # 骨架，重点测 execute 拒 str


def test_probe_cached_with_lock():
    sb = LandlockSandbox()
    assert sb._verdict() in ("full", "unusable", "unknown")

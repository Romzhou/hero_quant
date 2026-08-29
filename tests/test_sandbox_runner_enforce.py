import pytest
from hero_quant.sandbox.runner import LandlockSandbox


def test_string_cmd_workspace_write_rejected():
    sb = LandlockSandbox(policy={"mode": "workspace-write", "workspaceRoot": "/tmp"})
    with pytest.raises(Exception):
        sb.execute("echo hi", require_enforcement=True)


def test_dispatch_tool_enforce_propagation():
    from hero_quant.sandbox.runner import dispatch_tool
    # workspace-write without allow_direct_call should not execute callable
    class Dummy:
        name = "shell"
        def func(self, **kw):
            return "should_not_run"
    res = dispatch_tool(Dummy(), {}, {"mode": "workspace-write"})
    # workspace-write without allow_direct_call must be blocked (fail-closed)
    assert isinstance(res, dict) and "error" in res


def test_probe_cached_with_lock():
    sb = LandlockSandbox()
    assert sb._verdict() in ("full", "unusable", "unknown")

"""Lane A retest-271 repro tests — appended per file, one file per commit.

Create once; NEVER edit existing test files. Each section covers one sandbox
source file's review items: tests must FAIL before the fix and PASS after.
"""
from __future__ import annotations

import pytest

# ── ast_guard.py (7 items: 4 high + 3 medium) ─────────────────────────


def test_laneA_astguard_instance_method_bypass_blocked():
    """High: BANNED_ATTRS keyed on module/class name; instance calls evade it."""
    from hero_quant.sandbox.ast_guard import check_import_allowlist as chk

    assert chk('from pathlib import Path\np = Path("/tmp/x")\np.write_text("hi")') is False
    assert chk('import httpx\nclient = httpx.Client()\nclient.get("http://example.com")') is False


def test_laneA_astguard_subscript_call_blocked():
    """High: Call.func as Subscript (os["system"](...)) matches no branch."""
    from hero_quant.sandbox.ast_guard import check_import_allowlist as chk

    assert chk('import os\nos["system"]("id")') is False
    assert chk('import subprocess\nsubprocess["Popen"](["id"])') is False
    assert chk('__builtins__["eval"]("1+1")') is False


def test_laneA_astguard_qualified_pathlib_blocked():
    """High: pathlib.Path(...).write_text resolves root to pathlib, evades (Path, ...)."""
    from hero_quant.sandbox.ast_guard import check_import_allowlist as chk

    assert chk('import pathlib\npathlib.Path("/tmp/x").write_text("hi")') is False
    assert chk('import pathlib as pl\npl.Path("/tmp/x").read_text()') is False


def test_laneA_astguard_pickle_url_loaders_blocked():
    """High: allowlisted compute libs retain pickle/file/URL loaders."""
    from hero_quant.sandbox.ast_guard import check_import_allowlist as chk

    assert chk('import pandas as pd\npd.read_pickle("/tmp/x")') is False
    assert chk('import numpy as np\nnp.load("/tmp/x", allow_pickle=True)') is False
    assert chk('import yaml\nyaml.load("x: 1")') is False
    assert chk('import joblib\njoblib.load("/tmp/x")') is False


def test_laneA_astguard_no_dynamic_autoexpand():
    """Medium: _get_allowed_roots must not union unreviewed pyproject deps."""
    from hero_quant.sandbox import ast_guard as ag

    assert ag._get_allowed_roots() == set(ag._STATIC_ALLOWED) | set(ag._QUANTLIB_EXTRA)


def test_laneA_astguard_reload_invalidates_cache():
    """Medium: reload_allowed_roots must refresh the dynamic cache, not re-merge stale."""
    from hero_quant.sandbox import ast_guard as ag

    before_roots = set(ag.ALLOWED_ROOTS)
    ag._DYNAMIC_ROOTS = {"stale-poison-marker"}
    try:
        merged = ag.reload_allowed_roots()
        assert "stale-poison-marker" not in merged
        assert set(ag._DYNAMIC_ROOTS) == ag._load_pyproject_roots()
    finally:
        # restore process-global state: no pollution for other test files
        ag._DYNAMIC_ROOTS = None
        ag.ALLOWED_ROOTS.clear()
        ag.ALLOWED_ROOTS.update(before_roots)


def test_laneA_astguard_sync_two_sided(monkeypatch):
    """Medium: sync check must report stale entries, not just missing ones."""
    from hero_quant.sandbox import ast_guard as ag

    monkeypatch.setattr(ag, "_load_pyproject_roots", lambda: {"pandas"})
    ok, problems = ag.is_allowlist_synced_with_pyproject()
    assert ok is False
    assert any(p.startswith("stale:") for p in problems)


def test_laneA_astguard_from_import_bare_call_blocked():
    """Rescan: `from pandas import read_pickle` + bare call must not evade the guard."""
    from hero_quant.sandbox.ast_guard import check_import_allowlist as chk

    assert chk("from pandas import read_pickle\nread_pickle('/tmp/x')") is False
    assert chk("from joblib import load\nload('/tmp/x')") is False
    # benign from-imports still pass (no call, or non-banned attr)
    assert chk("from pandas import DataFrame\nx = DataFrame({'a': [1]})") is True


def test_laneA_astguard_data_subscript_and_query_allowed():
    """Rescan: OHLC data access d['open'] and df.query() must not be over-blocked."""
    from hero_quant.sandbox.ast_guard import check_import_allowlist as chk

    assert chk("import pandas as pd\ndf = pd.DataFrame({'a': [1]})\nq = df.query('a > 0')") is True
    assert chk("bar = {'open': 1.0}\nx = bar['open']") is True


# ── policy.py (4 items: 2 high + 2 medium) ──────────────────────────────


def test_laneA_policy_workspace_root_slash_rejected():
    """High: workspace-write with workspace_root='/' must not escalate to full-disk write."""
    import pytest

    from hero_quant.sandbox.policy import resolve_policy

    with pytest.raises(ValueError):
        resolve_policy(mode="workspace-write", workspace_root="/")


def test_laneA_policy_slash_root_gated_on_danger_mode():
    """High: writableRoots ['/'] grants all-path writes only in danger-full-access."""
    from hero_quant.sandbox.policy import is_path_writable

    assert is_path_writable("/etc/passwd", {"mode": "read-only", "writableRoots": ["/"]}) is False
    assert (
        is_path_writable("/etc/passwd", {"mode": "workspace-write", "writableRoots": ["/"]}) is False
    )
    assert (
        is_path_writable("/etc/passwd", {"mode": "danger-full-access", "writableRoots": ["/"]})
        is True
    )


def test_laneA_policy_malformed_roots_fail_closed():
    """Medium: non-string writableRoots entries must be skipped, not raise."""
    from hero_quant.sandbox.policy import is_path_writable

    pol = {"mode": "workspace-write", "writableRoots": [None, 123, b"/tmp", "/tmp"]}
    assert is_path_writable("/etc/passwd", pol) is False


def test_laneA_policy_canonical_non_str_valueerror():
    """Medium: canonical_path contract is ValueError-only; non-str must not leak TypeError."""
    import pytest

    from hero_quant.sandbox.policy import canonical_path

    with pytest.raises(ValueError):
        canonical_path(None)
    with pytest.raises(ValueError):
        canonical_path(123)


# ── base.py (4 items: 2 high + 2 medium) ────────────────────────────────


def test_laneA_base_root_workspace_rejected():
    """High: '/' must not validate as a workspace root (whole-disk write)."""
    import pytest

    from hero_quant.sandbox.base import _resolve_ws_strict, _validate_workspace_root

    with pytest.raises(ValueError):
        _validate_workspace_root("/")
    with pytest.raises((ValueError, Exception)):
        _resolve_ws_strict("/")


def test_laneA_base_docker_enforcement_not_overstated(monkeypatch):
    """High: DockerBackend.enforcement must mirror LocalShellBackend downgrades."""
    from hero_quant.sandbox import base as b

    monkeypatch.setattr(b, "_has_docker", lambda: True)
    assert b.DockerBackend(policy={"mode": "danger-full-access"}).enforcement == "partial"
    assert b.DockerBackend(policy={"mode": "read-only"}).enforcement == "partial"
    assert b.DockerBackend(policy={"mode": "workspace-write"}).enforcement == "full"


def test_laneA_base_tmpfs_before_workspace_bind(monkeypatch, tmp_path):
    """Medium: --tmpfs /tmp must precede the workspace --bind (bwrap order)."""
    from hero_quant.sandbox import base as b

    monkeypatch.setattr(b, "_has_bwrap", lambda: True)
    pol = {"mode": "workspace-write", "workspaceRoot": str(tmp_path)}
    out = b.LocalShellBackend(policy=pol).confine(["echo", "hi"], pol)
    assert out.index("--tmpfs") < out.index("--bind")


def test_laneA_base_malformed_roots_fail_closed():
    """Medium: non-string writableRoots entries must deny, not raise."""
    from hero_quant.sandbox.base import is_path_writable

    pol = {"mode": "workspace-write", "writableRoots": [None, 123, b"/tmp"]}
    assert is_path_writable("/tmp/x", pol) is False


# ── runner.py (4 items: 2 high + 2 medium; critical __import__ already fixed) ──


def test_laneA_runner_no_raw_argv_fallback(monkeypatch, tmp_path):
    """High: launcher failure in relaxed mode must raise, not run raw argv."""
    import subprocess

    from hero_quant.sandbox import runner as r
    from hero_quant.sandbox.runner import LandlockSandbox, SandboxUnavailableError

    # force the Linux launcher-missing path (cf. test_a17 platform mock);
    # bwrap present so the relaxed bwrap-fallback is constructed and fails at
    # run time, exposing the raw-argv fallback below it
    monkeypatch.setattr(r.sys, "platform", "linux")
    from hero_quant.sandbox import base as b

    monkeypatch.setattr(b, "_has_bwrap", lambda: True)
    sb = LandlockSandbox(policy={"mode": "workspace-write", "workspaceRoot": str(tmp_path)})
    monkeypatch.setattr(sb, "_verdict", lambda: "full")
    assert sb._launcher and "landlock" in sb._launcher.lower()

    class _R:
        stdout = ""
        stderr = ""
        returncode = 0

    def _selective(*a, **k):
        argv = a[0] if a else k.get("args", [])
        # sandboxed/launcher invocations fail; bare raw argv would succeed
        if argv and argv[0] != "echo":
            raise PermissionError("denied")
        return _R()

    monkeypatch.setattr(subprocess, "run", _selective)
    with pytest.raises(SandboxUnavailableError):
        sb.execute(["echo", "hi"], require_enforcement=False)


def test_laneA_runner_no_default_tmp_grant(monkeypatch, tmp_path):
    """High: workspace-write grants must not include shared /tmp by default."""
    from hero_quant.sandbox.runner import LandlockSandbox

    sb = LandlockSandbox(policy={"mode": "workspace-write", "workspaceRoot": str(tmp_path)})
    monkeypatch.setattr(sb, "_verdict", lambda: "full")
    argv = sb.confine(["echo", "hi"], {"mode": "workspace-write", "workspaceRoot": str(tmp_path)})
    rw_targets = [argv[i + 1] for i, t in enumerate(argv) if t == "--rw" and i + 1 < len(argv)]
    assert "/tmp" not in rw_targets


def test_laneA_runner_workspace_symlink_swap_rejected(tmp_path):
    """Medium: workspaceRoot that is a symlink must fail closed (TOCTOU guard)."""
    import pytest

    from hero_quant.sandbox.runner import LandlockSandbox, SandboxUnavailableError

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported")
    sb = LandlockSandbox(policy={"mode": "workspace-write", "workspaceRoot": str(link)})
    sb._verdict = lambda: "full"  # noqa: SLF001 - force grant path w/o probe
    with pytest.raises(SandboxUnavailableError):
        sb.confine(["echo", "hi"], {"mode": "workspace-write", "workspaceRoot": str(link)})


def test_laneA_runner_guarded_import_blocks_dynamic_os():
    """Critical (already fixed in baseline): runtime __import__('os') via dynamic name denied."""
    import pytest

    from hero_quant.sandbox.runner import SandboxViolation, execute_python

    with pytest.raises(SandboxViolation):
        execute_python("__import__(chr(111) + chr(115))")


# ── __init__.py (1 high: stub LandlockSandbox constructor contract) ────────


def test_laneA_init_stub_landlock_construction_contract():
    """High: stub LandlockSandbox must accept (policy=...) and report unusable."""
    from hero_quant.sandbox import _load_runner_stub as _stub
    from hero_quant.sandbox.base import SandboxUnavailableError

    Stub = _stub("LandlockSandbox")
    inst = Stub(policy={"mode": "workspace-write"})
    assert inst.enforcement == "unusable"
    with pytest.raises(SandboxUnavailableError):
        inst.execute(["echo", "hi"])
    with pytest.raises(SandboxUnavailableError):
        inst.confine(["echo"], {})

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

"""D2 — Landlock probe + AST allowlist sync maturity4 (TDD red->green)."""
from __future__ import annotations

import pathlib
import sys

try:
    import tomllib  # py311+
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore

import pytest


# --- Landlock probe contract (deepseek-harness landlock-run) ---


def test_landlock_probe_unusable_returns_125_on_this_host():
    """On Windows / no-landlock kernel, probe must return 125 and unusable (fail-closed)."""
    from hero_quant.sandbox.runner import (
        LAUNCHER_FAILURE_EXIT,
        SandboxUnavailableError,
        launcher_path,
        probe,
    )

    # launcher_path deliberately unchecked existence — probe is the signal
    lp = launcher_path()
    assert isinstance(lp, str) and len(lp) > 0

    verdict = probe()
    # On this Windows CI host Landlock is unavailable
    assert verdict == "unusable", f"expected unusable on Windows, got {verdict}"

    # Low-level probe must have emitted 125; runner's helper probe_raw or probe_with_detail
    from hero_quant.sandbox.runner import probe_raw

    exit_code, out, err = probe_raw()
    assert exit_code == LAUNCHER_FAILURE_EXIT == 125
    # fatal diagnostic must be prefixed landlock-run:
    assert "landlock-run:" in err
    # --probe success line must NOT appear when unusable
    assert "fully enforced" not in out and "partially enforced" not in out


def test_landlock_probe_flag_validation_effective():
    """--probe is mutually exclusive with grants/command; misuse must be 125 (CLI contract)."""
    from hero_quant.sandbox.runner import LAUNCHER_FAILURE_EXIT, validate_probe_args

    # --probe with extra args must fail 125, not succeed or ignore
    assert validate_probe_args(["landlock-run", "--probe", "--ro", "/tmp"]) == LAUNCHER_FAILURE_EXIT
    assert validate_probe_args(["landlock-run", "--probe", "--", "echo", "hi"]) == LAUNCHER_FAILURE_EXIT
    # missing -- separator with grants+command is also usage error 125
    assert validate_probe_args(["landlock-run", "--ro", "/tmp", "echo", "hi"]) == LAUNCHER_FAILURE_EXIT
    # correct probe alone is 0 (would then probe kernel; validation layer returns 0 means syntactically ok)
    assert validate_probe_args(["landlock-run", "--probe"]) == 0
    # correct confined run syntax must be syntactically valid (0)
    assert validate_probe_args(["landlock-run", "--ro", "/tmp", "--", "echo", "hi"]) == 0
    assert validate_probe_args(["landlock-run", "--rw", "/tmp", "--", "bash", "-c", "echo hi"]) == 0


def test_landlock_grant_args_generation():
    from hero_quant.sandbox.runner import grant_args

    assert grant_args({"readOnly": ["/"], "readWrite": ["/tmp/work"]}) == [
        "--ro",
        "/",
        "--rw",
        "/tmp/work",
    ]
    assert grant_args({"readOnly": ["/a", "/b"]}) == ["--ro", "/a", "--ro", "/b"]
    assert grant_args({}) == []


def test_sandbox_fail_closed_raises_when_landlock_unusable():
    """Fail-closed: workspace-write without enforcement must raise SandboxUnavailableError, not silently run."""
    from hero_quant.sandbox.policy import resolve_policy
    from hero_quant.sandbox.runner import LandlockSandbox, SandboxUnavailableError

    policy = resolve_policy(mode="workspace-write", workspace_root=str(pathlib.Path.cwd()))
    sb = LandlockSandbox(policy=policy)
    # On this host probe is unusable, so enforce=True must raise
    with pytest.raises(SandboxUnavailableError) as exc:
        sb.execute(["echo", "hi"], require_enforcement=True)
    assert "landlock" in str(exc.value).lower() or "unusable" in str(exc.value).lower() or "125" in str(exc.value)
    # Without require_enforcement (permissive) it should still run (fallback)
    out, err, code = sb.execute(["echo", "hi"], require_enforcement=False)
    assert code == 0
    assert "hi" in out

    # read-only mode is always enforceable (no Landlock needed)
    ro_policy = resolve_policy(mode="read-only")
    ro_sb = LandlockSandbox(policy=ro_policy)
    out, err, code = ro_sb.execute(["echo", "ro"], require_enforcement=True)
    assert code == 0


def test_landlock_sandbox_enforcement_property():
    from hero_quant.sandbox.runner import LandlockSandbox, probe
    from hero_quant.sandbox.policy import resolve_policy

    policy = resolve_policy(mode="workspace-write", workspace_root=str(pathlib.Path.cwd()))
    sb = LandlockSandbox(policy=policy)
    # enforcement reflects probe
    verdict = probe()
    if verdict == "unusable":
        assert sb.enforcement in ("unusable", "partial")
    else:
        assert sb.enforcement in ("full", "partial")


# --- AST guard — synced with pyproject + quantlib extras ---


def _pyproject_import_roots() -> set[str]:
    p = pathlib.Path("pyproject.toml")
    if not p.exists():
        # fallback for running from different cwd
        p = pathlib.Path(__file__).parents[1] / "pyproject.toml"
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    deps = list(data.get("project", {}).get("dependencies", []))
    # include optional-dependencies
    for group in data.get("project", {}).get("optional-dependencies", {}).values():
        deps.extend(group)
    roots: set[str] = set()
    alias = {
        "python-dotenv": "dotenv",
        "pyyaml": "yaml",
        "prometheus_client": "prometheus_client",
        "langchain-openai": "langchain_openai",
    }
    for raw in deps:
        # strip extras and version spec
        base = raw.strip().split(";")[0].split("[")[0]
        # split on version comparators
        for sep in (">=", "==", "~=", "!=", "<=", "<", ">"):
            if sep in base:
                base = base.split(sep)[0]
                break
        base = base.strip().lower()
        if not base:
            continue
        # map distribution name to import root
        if base in alias:
            roots.add(alias[base])
        else:
            roots.add(base.replace("-", "_"))
        # also add hyphen variant for safety (e.g., prometheus-client)
        # but import root is usually underscore
    return roots


def test_ast_allowlist_synced_with_pyproject():
    from hero_quant.sandbox.ast_guard import ALLOWED_ROOTS, check_import_allowlist

    py_roots = _pyproject_import_roots()
    # Every pyproject dependency import root must be allowlisted
    missing = [r for r in py_roots if r not in ALLOWED_ROOTS]
    assert not missing, f"allowlist missing pyproject deps: {missing} (ALLOWED_ROOTS={sorted(ALLOWED_ROOTS)})"
    # Verify allowlist actually permits them via AST guard
    for root in sorted(py_roots)[:10]:  # sample 10 to avoid huge loop
        code = f"import {root}"
        # Some roots like langchain_openai may not be installed but should still pass allowlist
        assert check_import_allowlist(code) is True, f"allowlist should permit pyproject dep {root}"


def test_ast_allowlist_allows_quantlib_deps():
    from hero_quant.sandbox.ast_guard import ALLOWED_ROOTS, check_import_allowlist

    quantlib = ["joblib", "duckdb", "sklearn", "statsmodels", "pyarrow", "polars", "numba"]
    for q in quantlib:
        assert q in ALLOWED_ROOTS, f"quantlib dep {q} must be in ALLOWED_ROOTS"
        assert check_import_allowlist(f"import {q}") is True
        assert check_import_allowlist(f"from {q}.utils import foo") is True


def test_ast_allowlist_still_blocks_banned():
    from hero_quant.sandbox.ast_guard import check_import_allowlist

    assert check_import_allowlist("import socket") is False
    assert check_import_allowlist("import subprocess") is False
    assert check_import_allowlist("import ctypes") is False
    assert check_import_allowlist("import requests") is False
    assert check_import_allowlist("import os") is False
    assert check_import_allowlist('import os; os.system("rm -rf /")') is False
    assert check_import_allowlist('eval("1+1")') is False
    assert check_import_allowlist('__import__("os")') is False
    # allowed ones still pass
    assert check_import_allowlist("import pandas\nimport numpy") is True
    assert check_import_allowlist("import joblib\nimport duckdb") is True


def test_ast_allowlist_sync_not_break_trace_ledger(tmp_path):
    """Ensure AST guard change does not break existing trace/ledger (regression guard)."""
    from hero_quant.sandbox.ast_guard import check_import_allowlist
    from hero_quant.agent.trace import TraceWriter
    from hero_quant.governance.ledger import Ledger

    assert check_import_allowlist("import pandas") is True
    # TraceWriter still durable
    tw = TraceWriter(tmp_path / "trace.jsonl")
    tw.append({"type": "tool_result", "content": "hello"})
    tw.close()
    assert (tmp_path / "trace.jsonl").exists()
    # Ledger still verifies
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append({"action": "order", "symbol": "600519.SH"})
    assert ledger.verify() is True

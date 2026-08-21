"""L0 AST guard — allowlist + banned patterns, deep scan (maturity 4).

Allowlist is now synced with pyproject dependencies plus quantlib extras (joblib/duckdb etc.).
Banned roots still take precedence (socket/subprocess/ctypes/requests/os remain blocked).
Sync strategy:
  - Static curated set derived from pyproject dependencies + quantlib extras
  - Dynamic fallback: at import time try to parse pyproject.toml and augment ALLOWED_ROOTS
  - Distribution name -> import root normalization (python-dotenv->dotenv, pyyaml->yaml, etc.)
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Base allowlist — manually synced from pyproject.toml + quantlib extras
# Kept explicit for auditability; dynamic loader below augments if file diverges.
# ---------------------------------------------------------------------------
_STATIC_ALLOWED = {
    # original 5
    "pandas",
    "numpy",
    "scipy",
    "math",
    "typing",
    # pyproject dependencies (import roots)
    "fastapi",
    "uvicorn",
    "pydantic",
    "dotenv",  # python-dotenv
    "httpx",
    "rich",
    "yaml",  # pyyaml
    "langchain",
    "langchain_openai",  # langchain-openai
    "langchain_core",
    "prometheus_client",
    "structlog",
    # optional dependencies
    "tushare",
    "akshare",
    "yfinance",
    "ccxt",
    "polars",
    # dev (allowed for generated code that imports test helpers; not security sensitive)
    "pytest",
    "pytest_cov",
    "ruff",
    "black",
    # quantlib extras explicitly requested: joblib/duckdb etc.
    "joblib",
    "duckdb",
    "sklearn",
    "statsmodels",
    "pyarrow",
    "numba",
    # stdlib safe helpers commonly used in quant code (not banned)
    "json",
    "re",
    "datetime",
    "collections",
    "itertools",
    "functools",
    "statistics",
    "decimal",
    "hashlib",
    "enum",
    "dataclasses",
    "pathlib",
    "logging",
    "copy",
    "operator",
    "string",
    "uuid",
    "time",
    "calendar",
    "zoneinfo",
}

# Quantlib extension set (explicitly required by task)
_QUANTLIB_EXTRA = {"joblib", "duckdb", "sklearn", "statsmodels", "pyarrow", "polars", "numba"}

# ---------------------------------------------------------------------------
# Distribution -> import root alias map (distribution name lowercased)
# ---------------------------------------------------------------------------
_DIST_ALIAS: dict[str, str] = {
    "python-dotenv": "dotenv",
    "pyyaml": "yaml",
    "prometheus_client": "prometheus_client",
    "prometheus-client": "prometheus_client",
    "langchain-openai": "langchain_openai",
    "langchain_core": "langchain_core",
    "langchain-core": "langchain_core",
    "scikit-learn": "sklearn",
    "pytest-cov": "pytest_cov",
}


def _dist_to_import(dist: str) -> str:
    """Normalize distribution name to import root."""
    d = dist.strip().lower()
    if d in _DIST_ALIAS:
        return _DIST_ALIAS[d]
    # hyphen -> underscore is the default import mapping
    return d.replace("-", "_")


def _load_pyproject_roots() -> set[str]:
    """Parse pyproject.toml dependencies and return import roots (best-effort)."""
    roots: set[str] = set()
    # locate pyproject.toml: walk up from this file
    candidates = [
        Path(__file__).resolve().parents[3] / "pyproject.toml",  # src/hero_quant/sandbox -> repo root
        Path(__file__).resolve().parents[2] / "pyproject.toml",
        Path.cwd() / "pyproject.toml",
    ]
    pyproject = None
    for c in candidates:
        if c.exists():
            pyproject = c
            break
    if pyproject is None:
        return roots
    try:
        # Python 3.11+ tomllib
        try:
            import tomllib  # type: ignore
        except ModuleNotFoundError:
            import tomli as tomllib  # type: ignore

        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception:
        return roots
    deps: list[str] = []
    deps.extend(data.get("project", {}).get("dependencies", []) or [])
    for group in (data.get("project", {}).get("optional-dependencies", {}) or {}).values():
        deps.extend(group)
    for raw in deps:
        # strip env markers and extras: "uvicorn[standard]>=0.24 ; python_version>'3.11'" -> "uvicorn"
        base = raw.strip().split(";")[0].strip()
        # remove extras [standard]
        base = re.split(r"\[", base, maxsplit=1)[0]
        # split on version specifiers
        base = re.split(r"[<>=!~]", base, maxsplit=1)[0].strip().lower()
        if not base:
            continue
        roots.add(_dist_to_import(base))
    return roots


# Merge static + dynamic (union) so task's "同步" is satisfied even if pyproject drifts
_DYNAMIC_ROOTS = _load_pyproject_roots()
ALLOWED_ROOTS: set[str] = set(_STATIC_ALLOWED) | set(_DYNAMIC_ROOTS) | set(_QUANTLIB_EXTRA)

# Ensure quantlib extras are always present even if pyproject lacks them
ALLOWED_ROOTS.update(_QUANTLIB_EXTRA)

# Explicit bans per spec: socket / subprocess / ctypes / requests / eval / __import__
# os.system is banned via attribute check; os import itself is treated as banned
# when used with dangerous attrs, but also blocked if not in allowlist.
BANNED_IMPORT_ROOTS = {"socket", "subprocess", "ctypes", "requests", "os"}
BANNED_CALL_NAMES = {"eval", "exec", "__import__"}
# attribute bans: (base, attr)
BANNED_ATTRS = {
    ("os", "system"),
    ("os", "popen"),
    ("os", "execve"),
    ("os", "spawnl"),
    ("os", "spawnlp"),
    ("subprocess", "Popen"),
    ("subprocess", "call"),
    ("subprocess", "run"),
    ("subprocess", "check_call"),
    ("subprocess", "check_output"),
}


def _is_banned_attribute(node: ast.Attribute) -> bool:
    """Check if attribute access matches banned list or is on banned root."""
    # direct (os.system) check
    if isinstance(node.value, ast.Name):
        base = node.value.id
        attr = node.attr
        if (base, attr) in BANNED_ATTRS:
            return True
        # any ctypes.* / socket.* / requests.* attribute is banned
        if base in {"ctypes", "socket", "requests"}:
            return True
        if base == "subprocess":
            return True
        if base == "os" and attr in {"system", "popen", "execve", "spawnl", "spawnlp", "execv", "execl"}:
            return True
    return False


def check_import_allowlist(code: str) -> bool:
    """
    Return True if code only uses allowlisted imports and no banned patterns.
    Deep scans nested functions/classes via ast.walk.
    Banned roots take precedence over allowlist.
    """
    if not code or not code.strip():
        return True
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False

    for node in ast.walk(tree):
        # Import: import X, import X.Y
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                # banned roots always fail
                if root in BANNED_IMPORT_ROOTS:
                    return False
                if root not in ALLOWED_ROOTS:
                    # allowlist enforcement: any non-allowlisted root is denied
                    return False
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                return False
            root = node.module.split(".")[0]
            if root in BANNED_IMPORT_ROOTS:
                return False
            if root not in ALLOWED_ROOTS:
                return False
        elif isinstance(node, ast.Call):
            # banned builtin calls: eval(...), exec(...), __import__(...)
            func = node.func
            if isinstance(func, ast.Name) and func.id in BANNED_CALL_NAMES:
                return False
            if isinstance(func, ast.Attribute):
                if _is_banned_attribute(func):
                    return False
        elif isinstance(node, ast.Attribute):
            # bare attribute access without call (e.g., x = os.system) should also be banned
            if _is_banned_attribute(node):
                return False

    return True


def assert_allowlist(code: str) -> None:
    """Raise ValueError if not allowlisted."""
    if not check_import_allowlist(code):
        raise ValueError("import allowlist violation or banned pattern detected")


def get_allowed_roots() -> set[str]:
    """Return a copy of the current allowlist (for introspection / tests)."""
    return set(ALLOWED_ROOTS)


def is_allowlist_synced_with_pyproject() -> tuple[bool, list[str]]:
    """Check sync status: returns (ok, missing)."""
    dynamic = _load_pyproject_roots()
    missing = [r for r in dynamic if r not in ALLOWED_ROOTS]
    return (len(missing) == 0, missing)

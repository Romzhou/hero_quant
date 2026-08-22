"""沙箱包入口 — 汇集 L0 AST 守卫、L1 策略、抽象沙箱与 Landlock 探针。

对外提供统一导入面，缺失 runner 时以兼容存根兜底，保证基础功能可用。
"""

from .ast_guard import ALLOWED_ROOTS, assert_allowlist, check_import_allowlist, get_allowed_roots
from .base import BaseSandbox, DockerBackend, LocalShellBackend
from .policy import VALID_MODES, canonical_path, is_path_writable, resolve_policy

try:
    from .runner import (
        LAUNCHER_BIN,
        LAUNCHER_FAILURE_EXIT,
        LandlockSandbox,
        SandboxUnavailableError,
        grant_args,
        launcher_path,
        probe,
        probe_raw,
        validate_probe_args,
    )
except Exception:  # pragma: no cover — runner 缺失时不影响基础沙箱功能
    LAUNCHER_BIN = "landlock-run"  # type: ignore
    LAUNCHER_FAILURE_EXIT = 125  # type: ignore

    class SandboxUnavailableError(RuntimeError):  # type: ignore
        pass

    def launcher_path(*a, **kw):  # type: ignore
        return "landlock-run"

    def grant_args(*a, **kw):  # type: ignore
        return []

    def probe(*a, **kw):  # type: ignore
        return "unusable"

    def probe_raw(*a, **kw):  # type: ignore
        return (125, "", "landlock-run: unusable\n")

    def validate_probe_args(*a, **kw):  # type: ignore
        return 125

    class LandlockSandbox(BaseSandbox):  # type: ignore
        pass


__all__ = [
    "check_import_allowlist",
    "assert_allowlist",
    "ALLOWED_ROOTS",
    "get_allowed_roots",
    "resolve_policy",
    "canonical_path",
    "is_path_writable",
    "VALID_MODES",
    "BaseSandbox",
    "LocalShellBackend",
    "DockerBackend",
    "LandlockSandbox",
    "SandboxUnavailableError",
    "LAUNCHER_FAILURE_EXIT",
    "LAUNCHER_BIN",
    "launcher_path",
    "grant_args",
    "probe",
    "probe_raw",
    "validate_probe_args",
]

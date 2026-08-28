"""沙箱包入口 — 汇集 L0 AST 守卫、L1 策略、抽象沙箱与 Landlock 探针。

对外提供统一导入面，缺失 runner 时以兼容存根兜底，保证基础功能可用。
"""

from .ast_guard import (
    ALLOWED_ROOTS,
    SandboxViolation,
    assert_allowlist,
    check_import_allowlist,
    check_source,
    get_allowed_roots,
)
from .base import BaseSandbox, DockerBackend, LocalShellBackend
from .policy import VALID_MODES, canonical_path, is_path_writable, resolve_policy

try:
    from .runner import (
        LAUNCHER_BIN,
        LAUNCHER_FAILURE_EXIT,
        LandlockSandbox,
        SandboxUnavailableError,
        SandboxViolation as _RunnerSandboxViolation,
        check_source as _runner_check_source,
        execute_python,
        grant_args,
        launcher_path,
        probe,
        probe_raw,
        validate_probe_args,
    )

    # 保持便捷导出的统一身份：包级 SandboxViolation 即 ast_guard 的同一类型
    assert _RunnerSandboxViolation is SandboxViolation  # type: ignore[attr-defined]
    SandboxViolation = _RunnerSandboxViolation  # type: ignore
    check_source = _runner_check_source  # type: ignore
except (ImportError, ModuleNotFoundError):  # pragma: no cover — runner 缺失时不影响基础沙箱功能
    from .base import SandboxUnavailableError  # type: ignore[import-not-found] # noqa: F401

    LAUNCHER_BIN = "landlock-run"  # type: ignore
    LAUNCHER_FAILURE_EXIT = 125  # type: ignore

    def launcher_path(*a, **kw):  # type: ignore
        return "landlock-run"

    def grant_args(*a, **kw):  # type: ignore
        raise SandboxUnavailableError("sandbox unavailable: runner not installed")

    def probe(*a, **kw):  # type: ignore
        raise SandboxUnavailableError("sandbox unavailable: runner not installed")

    def probe_raw(*a, **kw):  # type: ignore
        raise SandboxUnavailableError("sandbox unavailable: runner not installed")

    def validate_probe_args(*a, **kw):  # type: ignore
        raise SandboxUnavailableError("sandbox unavailable: runner not installed")

    # AST 守卫保持 fail-closed：委托 canonical ast_guard，保持异常身份不 fork
    # SandboxViolation / check_source 已在顶层从 ast_guard 导入，此处不再重定义

    class LandlockSandbox(BaseSandbox):  # type: ignore
        def execute(self, *a, **kw):  # type: ignore
            raise SandboxUnavailableError("sandbox unavailable: runner not installed")

        def confine(self, argv, policy=None):  # type: ignore
            raise SandboxUnavailableError("sandbox unavailable: runner not installed")

        def execute_python(self, source, *a, **kw):  # type: ignore
            check_source(source)
            raise SandboxUnavailableError("sandbox unavailable: runner not installed")

    def execute_python(source, *a, **kw):  # type: ignore
        check_source(source)
        raise SandboxUnavailableError("sandbox unavailable: runner not installed")


__all__ = [
    "check_import_allowlist",
    "assert_allowlist",
    "check_source",
    "SandboxViolation",
    "execute_python",
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

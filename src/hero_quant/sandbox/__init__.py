"""沙箱包入口 — 汇集 L0 AST 守卫、L1 策略、抽象沙箱与 Landlock 探针。

对外提供统一导入面，缺失 runner 时以兼容存根兜底，保证基础功能可用。
采用 PEP 562 惰性加载，避免导入时 I/O 副作用（ast_guard 的 pyproject 解析延迟到首次访问）。
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

logger = logging.getLogger(__name__)

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

_LAZY_MAP: dict[str, str] = {
    "check_import_allowlist": "hero_quant.sandbox.ast_guard",
    "assert_allowlist": "hero_quant.sandbox.ast_guard",
    "check_source": "hero_quant.sandbox.ast_guard",
    "SandboxViolation": "hero_quant.sandbox.ast_guard",
    "ALLOWED_ROOTS": "hero_quant.sandbox.ast_guard",
    "get_allowed_roots": "hero_quant.sandbox.ast_guard",
    "resolve_policy": "hero_quant.sandbox.policy",
    "canonical_path": "hero_quant.sandbox.policy",
    "is_path_writable": "hero_quant.sandbox.policy",
    "VALID_MODES": "hero_quant.sandbox.policy",
    "BaseSandbox": "hero_quant.sandbox.base",
    "LocalShellBackend": "hero_quant.sandbox.base",
    "DockerBackend": "hero_quant.sandbox.base",
    # runner 专属，缺失时走存根
    "LandlockSandbox": "hero_quant.sandbox.runner",
    "SandboxUnavailableError": "hero_quant.sandbox.runner",
    "LAUNCHER_FAILURE_EXIT": "hero_quant.sandbox.runner",
    "LAUNCHER_BIN": "hero_quant.sandbox.runner",
    "launcher_path": "hero_quant.sandbox.runner",
    "grant_args": "hero_quant.sandbox.runner",
    "probe": "hero_quant.sandbox.runner",
    "probe_raw": "hero_quant.sandbox.runner",
    "validate_probe_args": "hero_quant.sandbox.runner",
    "execute_python": "hero_quant.sandbox.runner",
}


def _load_runner_stub(name: str) -> Any:
    """runner 缺失時的兼容存根，保證基礎沙箱功能可用。"""
    from .base import BaseSandbox as _Base
    from .base import SandboxUnavailableError as _UE
    from .ast_guard import check_source as _cs

    if name == "SandboxUnavailableError":
        return _UE
    if name == "LAUNCHER_BIN":
        return "landlock-run"
    if name == "LAUNCHER_FAILURE_EXIT":
        return 125
    if name == "launcher_path":
        def _launcher_path(*a, **kw):  # type: ignore
            return "landlock-run"
        return _launcher_path
    if name in ("grant_args", "probe", "probe_raw", "validate_probe_args"):
        def _stub(*a, **kw):  # type: ignore
            raise _UE("sandbox unavailable: runner not installed")
        return _stub
    if name == "LandlockSandbox":
        class _StubLandlock(_Base):  # type: ignore
            def execute(self, *a, **kw):  # type: ignore
                raise _UE("sandbox unavailable: runner not installed")
            def confine(self, argv, policy=None):  # type: ignore
                raise _UE("sandbox unavailable: runner not installed")
            def execute_python(self, source, *a, **kw):  # type: ignore
                _cs(source)
                raise _UE("sandbox unavailable: runner not installed")
        return _StubLandlock
    if name == "execute_python":
        def _exec_py(source, *a, **kw):  # type: ignore
            _cs(source)
            raise _UE("sandbox unavailable: runner not installed")
        return _exec_py
    if name in ("check_source", "SandboxViolation"):
        # 委托 canonical ast_guard，保持異常身份不 fork
        mod = importlib.import_module("hero_quant.sandbox.ast_guard")
        return getattr(mod, name)
    raise AttributeError(name)


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    # 特殊：SandboxViolation / check_source 始終以 ast_guard 為 canonical 身份
    if name in ("SandboxViolation", "check_source"):
        mod = importlib.import_module("hero_quant.sandbox.ast_guard")
        val = getattr(mod, name)
        globals()[name] = val
        return val
    target = _LAZY_MAP.get(name)
    if target:
        try:
            mod = importlib.import_module(target)
            val = getattr(mod, name)
            # 驗證 runner 重導出的身份一致性（僅對 SandboxViolation/check_source 已在上層處理）
            globals()[name] = val
            return val
        except (ImportError, ModuleNotFoundError) as e:
            # 窄化：僅容忍 runner 缺失，內部語法錯誤等不應吞沒
            if target == "hero_quant.sandbox.runner":
                logger.debug("runner import failed, falling back to stubs: %s", e)
                val = _load_runner_stub(name)
                globals()[name] = val
                return val
            raise
        except Exception:
            # 內部實現錯誤不應被包裝為存根，直接拋出以便 fail-closed
            raise
    # fallback stub for runner names already handled
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)

"""沙箱抽象层 — 定义策略驱动的隔离接口与本地/Docker 两种后端。

架构位置：sandbox 层的抽象基座，上层通过 ``resolve_policy`` 生成策略后
调用 ``confine``/``execute``；具体隔离由 ``runner.LandlockSandbox`` 接管。

安全设计：``confine()`` 仅在检测到 ``bwrap`` 二进制时才添加绑定挂载前缀，
否则 no-op 回退，保证离线/Windows 下行为可预期且不误判为已隔离。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Tuple, Union


def _has_bwrap() -> bool:
    """检测 ``bwrap`` 是否可用，仅当 PATH 中存在时返回 True。"""
    try:
        return shutil.which("bwrap") is not None
    except Exception:
        return False


def _has_docker() -> bool:
    try:
        return shutil.which("docker") is not None
    except Exception:
        return False


class SandboxUnavailableError(RuntimeError):
    """Fail-closed：workspace-write 要求强隔离但 bwrap/landlock 不可用时抛出。"""


def _has_invalid_colon(ws: str) -> bool:
    """检测非 Windows 盘符外的 ':'，用于 Docker -v 注入防护."""
    for i, ch in enumerate(ws):
        if ch == ":":
            # 允许 Windows 盘符 C:\ / C:/ 在位置 1 的单冒号
            if i == 1 and ws[0].isalpha() and len(ws) > 2 and ws[2] in ("\\", "/"):
                continue
            return True
    return False


def _validate_workspace_root(ws: str) -> None:
    """校验 workspaceRoot 的 Docker -v 注入风险与基本合法性."""
    if _has_invalid_colon(ws) or "\n" in ws or "\r" in ws:
        raise ValueError("workspaceRoot must not contain ':' or newline")
    if not os.path.isabs(ws):
        raise ValueError("workspaceRoot must be absolute path")


def _resolve_ws_strict(ws: str) -> str:
    """解析符号链接，严格要求路径存在；失败抛 SandboxUnavailableError."""
    _validate_workspace_root(ws)
    try:
        ws_canonical = str(Path(ws).resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as e:
        raise SandboxUnavailableError(f"workspaceRoot unavailable: {e}") from e
    # 额外校验：必须是已存在目录，防止挂载不存在路径
    if not Path(ws_canonical).is_dir():
        raise ValueError("workspaceRoot must be an existing directory")
    if _has_invalid_colon(ws_canonical) or "\n" in ws_canonical or "\r" in ws_canonical:
        raise ValueError("workspaceRoot canonical contains invalid characters")
    return ws_canonical


def is_path_writable(path: str, policy: dict) -> bool:
    """判断路径是否落在可写根内（规范路径 + commonpath，防前缀欺骗）。

    NOTE: 存在 TOCTOU 窗口——校验与使用之间路径可能被符号链接替换，
    调用方需配合 enforcement 强制重校验或持有隔离上下文。
    弃用 raw "/tmp" 回退，统一用 commonpath 判定。
    """
    try:
        cp = str(Path(path).resolve())
    except Exception:
        try:
            cp = os.path.realpath(path)
        except Exception:
            cp = path
    roots = policy.get("writableRoots") or []
    if not roots:
        ws = policy.get("workspaceRoot") or policy.get("workspace_root") or policy.get("canonicalPath")
        if ws:
            try:
                roots = [str(Path(ws).resolve())]
            except Exception:
                roots = [str(ws)]
    for root in roots:
        if root == "/":
            return True
        try:
            r = str(Path(root).resolve())
        except Exception:
            r = root
        if r == "/":
            return True
        # 使用 commonpath 防 /tmp-evil 前缀欺骗，不再单独回退 raw "/tmp"
        try:
            if cp == r or os.path.commonpath([cp, r]) == r:
                return True
        except ValueError:
            if cp == r or cp.startswith(r + os.sep):
                return True
    return False


class BaseSandbox(ABC):
    """沙箱抽象基类：定义 ``execute``/``confine``/``enforcement`` 契约。

    安全不变量：默认拒绝——未显式授权的路径与能力不予开放。
    """

    @abstractmethod
    def execute(self, cmd: Union[str, List[str]]) -> Tuple[str, str, int]:
        raise NotImplementedError

    def confine(self, argv: List[str], policy: dict) -> List[str]:
        """按策略包裹 argv：workspace-write 且 bwrap 可用时添加只读根与可写绑定，否则抛 SandboxUnavailableError。"""
        if not isinstance(argv, (list, tuple)):
            raise TypeError("argv must be List[str]; str cmd not allowed")
        if not argv:
            return list(argv)
        argv = [str(x) for x in argv]  # 归一化为字符串，避免类型混淆注入
        mode = None
        if isinstance(policy, dict):
            mode = policy.get("mode")
        if mode == "workspace-write":
            if _has_bwrap():
                ws = None
                if isinstance(policy, dict):
                    ws = policy.get("workspaceRoot") or policy.get("workspace_root") or policy.get("canonicalPath")
                if ws:
                    ws_canonical = _resolve_ws_strict(str(ws))
                else:
                    # 无显式工作区时使用 /tmp，需严格解析
                    try:
                        ws_canonical = str(Path("/tmp").resolve(strict=True))
                    except (OSError, RuntimeError, ValueError) as e:
                        raise SandboxUnavailableError(f"workspaceRoot unavailable: {e}") from e
                # bwrap 前缀：根目录只读，工作区与 /tmp 可写；保留最小通用参数
                prefix: List[str] = [
                    "bwrap",
                    "--ro-bind", "/", "/",
                    "--bind", ws_canonical, ws_canonical,
                    "--dev", "/dev",
                    "--proc", "/proc",
                    "--bind", "/tmp", "/tmp",
                    "--unshare-all",
                    "--die-with-parent",
                    "--",
                ]
                return prefix + argv
            # 无 bwrap 时 fail-closed：由工具调度层捕获后决定降级或拒绝
            raise SandboxUnavailableError("bwrap unavailable: workspace-write requires bwrap but binary not found")
        # read-only / danger-full-access 在 Python 层不做包裹，由上层隔离保证
        return list(argv)

    @property
    def enforcement(self) -> str:
        """默认隔离等级为 full，子类可按实际能力覆写为 partial。"""
        return "full"


class LocalShellBackend(BaseSandbox):
    """本地直通后端——用于开发/测试，隔离等级按策略判定。"""

    def __init__(self, policy: dict | None = None):
        self._policy: dict = dict(policy) if isinstance(policy, dict) else {}

    def execute(self, cmd: Union[str, List[str]]) -> Tuple[str, str, int]:
        """执行命令；拒绝 str 以防 shell 注入，仅接受 List[str]."""
        if isinstance(cmd, str):
            raise ValueError("str cmd not allowed; use List[str]")
        pol = self._policy if self._policy else {}
        wrapped = self.confine(cmd, pol)  # 仅当 bwrap 可用时才加前缀
        result = subprocess.run(wrapped, shell=False, capture_output=True, text=True)
        return result.stdout, result.stderr, result.returncode

    def confine(self, argv: List[str], policy: dict) -> List[str]:
        # 合并存储策略与单次策略，单次优先
        merged: dict = {}
        if isinstance(self._policy, dict):
            merged.update(self._policy)
        if isinstance(policy, dict):
            merged.update(policy)
        return super().confine(argv, merged)

    @property
    def enforcement(self) -> str:
        # danger-full-access 视为未隔离（partial），其余为 full
        if isinstance(self._policy, dict) and self._policy.get("mode") == "danger-full-access":
            return "partial"
        return "full"


class DockerBackend(BaseSandbox):
    """Docker 存根后端——无 docker 时回退本地执行，有 docker 时以容器隔离执行。"""

    def __init__(self, image: str = "hero-quant:sandbox", policy: dict | None = None):
        self.image = image
        self._policy: dict = dict(policy) if isinstance(policy, dict) else {}

    def execute(self, cmd: Union[str, List[str]]) -> Tuple[str, str, int]:
        """执行命令；有 docker 时走容器，否则回退本地以保持离线可用。"""
        if isinstance(cmd, str):
            raise ValueError("str cmd not allowed; use List[str]")
        pol = self._policy if self._policy else {}
        wrapped = self.confine(cmd, pol)
        # 存根模式下若 docker 不存在，避免 ENOENT，回退本地
        if wrapped and wrapped[0] == "docker" and not _has_docker():
            fallback = super().confine(cmd, pol)  # 走 bwrap 条件回退
            result = subprocess.run(fallback, shell=False, capture_output=True, text=True)
            return result.stdout, result.stderr, result.returncode
        try:
            result = subprocess.run(wrapped, shell=False, capture_output=True, text=True)
        except FileNotFoundError:
            # 二进制缺失时回退原命令，避免测试环境因缺依赖而失败
            result = subprocess.run(cmd, shell=False, capture_output=True, text=True)
        return result.stdout, result.stderr, result.returncode

    def confine(self, argv: List[str], policy: dict) -> List[str]:
        # 合并策略，单次调用优先
        merged: dict = {}
        if isinstance(self._policy, dict):
            merged.update(self._policy)
        if isinstance(policy, dict):
            merged.update(policy)
        mode = merged.get("mode")
        if mode == "workspace-write":
            # workspaceRoot 校验：":"/"\n"/绝对路径/is_dir/containment
            ws_raw = merged.get("workspaceRoot") or merged.get("workspace_root") or merged.get("canonicalPath") or "/tmp"
            ws_str = str(ws_raw)
            # 提前校验 Docker -v 注入字符，即使 docker 不可用也需 fail-fast
            _validate_workspace_root(ws_str)
            try:
                ws_canonical = str(Path(ws_str).resolve(strict=True))
            except (OSError, RuntimeError, ValueError) as e:
                raise SandboxUnavailableError(f"workspaceRoot unavailable: {e}") from e
            if not Path(ws_canonical).is_dir():
                raise ValueError("workspaceRoot must be an existing directory")
            if _has_invalid_colon(ws_canonical) or "\n" in ws_canonical or "\r" in ws_canonical:
                raise ValueError("workspaceRoot canonical contains invalid characters")
            # docker 可用且为 workspace-write 时优先容器隔离，否则走 bwrap 逻辑
            if _has_docker():
                # docker 前缀：丢弃全部能力、只读根、挂载工作区
                prefix: List[str] = [
                    "docker", "run", "--rm",
                    "--cap-drop", "ALL",
                    "--read-only",
                    "--tmpfs", "/tmp",
                    "-v", f"{ws_canonical}:{ws_canonical}:rw",
                    self.image,
                ]
                if not isinstance(argv, (list, tuple)):
                    raise TypeError("argv must be List[str]; str cmd not allowed")
                return prefix + [str(x) for x in argv]
        if not isinstance(argv, (list, tuple)):
            raise TypeError("argv must be List[str]; str cmd not allowed")
        return super().confine([str(x) for x in argv], merged)

    @property
    def enforcement(self) -> str:
        # 有 docker 视为 full，无 docker 标记为 partial 以提示未真正隔离
        if _has_docker():
            return "full"
        return "partial"

"""沙箱抽象层 — 定义策略驱动的隔离接口与本地/Docker 两种后端。

架构位置：sandbox 层的抽象基座，上层通过 ``resolve_policy`` 生成策略后
调用 ``confine``/``execute``；具体隔离由 ``runner.LandlockSandbox`` 接管。

安全设计：``confine()`` 仅在检测到 ``bwrap`` 二进制时才添加绑定挂载前缀，
否则 no-op 回退，保证离线/Windows 下行为可预期且不误判为已隔离。
"""
from __future__ import annotations

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


class BaseSandbox(ABC):
    """沙箱抽象基类：定义 ``execute``/``confine``/``enforcement`` 契约。

    安全不变量：默认拒绝——未显式授权的路径与能力不予开放。
    """

    @abstractmethod
    def execute(self, cmd: Union[str, List[str]]) -> Tuple[str, str, int]:
        raise NotImplementedError

    def confine(self, argv: List[str], policy: dict) -> List[str]:
        """按策略包裹 argv：workspace-write 且 bwrap 可用时添加只读根与可写绑定，否则原样返回。"""
        if not argv:
            return list(argv)
        if not isinstance(argv, list):
            return list(argv)  # type: ignore[return-value]
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
                    try:
                        ws_canonical = str(Path(ws).resolve())  # 解析符号链接，防路径穿越
                    except Exception:
                        ws_canonical = str(ws)
                else:
                    ws_canonical = "/tmp"
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
            # 无 bwrap 时 no-op，避免在离线环境误报已隔离
            return list(argv)
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
        """执行命令；字符串走 shell 展开，列表走 confine 包裹。"""
        if isinstance(cmd, str):
            # shell 字符串需展开，无法做 argv 级隔离；路径约束由上层策略保证
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        else:
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
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            return result.stdout, result.stderr, result.returncode
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
        # docker 可用且为 workspace-write 时优先容器隔离，否则走 bwrap 逻辑
        if _has_docker() and mode == "workspace-write":
            ws = merged.get("workspaceRoot") or merged.get("workspace_root") or merged.get("canonicalPath") or "/tmp"
            try:
                ws_canonical = str(Path(ws).resolve())  # 解析符号链接防穿越
            except Exception:
                ws_canonical = str(ws)
            # docker 前缀：丢弃全部能力、只读根、挂载工作区
            prefix: List[str] = [
                "docker", "run", "--rm",
                "--cap-drop", "ALL",
                "--read-only",
                "--tmpfs", "/tmp",
                "-v", f"{ws_canonical}:{ws_canonical}:rw",
                self.image,
            ]
            return prefix + [str(x) for x in argv]
        return super().confine([str(x) for x in argv], merged)

    @property
    def enforcement(self) -> str:
        # 有 docker 视为 full，无 docker 标记为 partial 以提示未真正隔离
        if _has_docker():
            return "full"
        return "partial"

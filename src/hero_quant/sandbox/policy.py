"""沙箱策略层 — 解析 mode / canonicalPath / writableRoots，统一路径可写性判定。

安全设计：所有路径以 ``Path.resolve()`` 归一化后比较，防止符号链接与 ``..``
绕过；``workspace-write`` 仅开放工作区与 ``/tmp``，其余默认只读。
"""
import os
from pathlib import Path

VALID_MODES = {"read-only", "workspace-write", "danger-full-access"}


def canonical_path(p: str) -> str:
    """返回路径的真实规范路径（解析符号链接），失败时回退到原路径。"""
    try:
        # 使用 Path.resolve() 而非 realpath，缺失路径也不抛异常
        return str(Path(p).resolve())
    except Exception:
        try:
            return os.path.realpath(p)  # 兜底，仍尝试解析
        except Exception:
            return str(Path(p).resolve())


def resolve_policy(mode: str, workspace_root: str | None = None) -> dict:
    """解析沙箱策略，返回包含 mode/canonicalPath/writableRoots/enforcement 的字典。"""
    if mode not in VALID_MODES:
        raise ValueError(f"invalid mode: {mode}, expected one of {VALID_MODES}")

    policy: dict = {"mode": mode}

    if workspace_root is not None:
        cp = canonical_path(workspace_root)
        policy["workspaceRoot"] = cp
        policy["canonicalPath"] = cp
        policy["workspace_root"] = cp  # snake alias for convenience
    else:
        # still expose workspaceRoot as empty for read-only if not provided
        if mode == "workspace-write":
            raise ValueError("workspace_root required for workspace-write mode")

    if mode == "workspace-write":
        roots = []
        if workspace_root is not None:
            roots.append(policy["workspaceRoot"])
        # /tmp 始终可写，兼容符号链接场景同时保留字面量与规范路径
        tmp_canonical = canonical_path("/tmp") if os.path.exists("/tmp") else "/tmp"
        if tmp_canonical not in roots:
            roots.append(tmp_canonical)
        if "/tmp" not in roots:
            roots.append("/tmp")
        # 去重并保持顺序
        seen = set()
        uniq = []
        for r in roots:
            if r not in seen:
                uniq.append(r)
                seen.add(r)
        policy["writableRoots"] = uniq
        policy["enforcement"] = "full"
    elif mode == "read-only":
        tmp_canonical = canonical_path("/tmp") if os.path.exists("/tmp") else "/tmp"
        policy["writableRoots"] = [tmp_canonical] if tmp_canonical == "/tmp" else [tmp_canonical, "/tmp"]
        if len(policy["writableRoots"]) == 2 and policy["writableRoots"][0] == policy["writableRoots"][1]:
            policy["writableRoots"] = ["/tmp"]
        policy["enforcement"] = "full"
        if "canonicalPath" not in policy:
            try:
                policy["canonicalPath"] = str(Path.cwd().resolve())
            except Exception:
                policy["canonicalPath"] = str(Path(".").resolve())
    else:  # danger-full-access
        policy["writableRoots"] = ["/"]  # 全盘可写，仅用于显式危险模式
        policy["enforcement"] = "partial"  # 标记为未强隔离
        if "workspaceRoot" not in policy and workspace_root is None:
            if "canonicalPath" not in policy:
                try:
                    policy["canonicalPath"] = str(Path.cwd().resolve())
                except Exception:
                    policy["canonicalPath"] = str(Path(".").resolve())

    return policy


def is_path_writable(path: str, policy: dict) -> bool:
    """判断路径是否落在可写根内（规范路径前缀匹配，防穿越）。"""
    cp = canonical_path(path)  # 先归一化，消除符号链接与 .. 干扰
    for root in policy.get("writableRoots", []):
        r = canonical_path(root) if root != "/" else "/"
        if r == "/":
            return True  # danger-full-access 全盘可写
        if cp == r or cp.startswith(r + os.sep):
            return True
        # 兼容 Windows 下 /tmp 路径分隔符差异
        if root == "/tmp" and (cp == "/tmp" or cp.startswith("/tmp/") or cp.startswith("/tmp\\")):
            return True
    return False

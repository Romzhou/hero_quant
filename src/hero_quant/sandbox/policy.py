"""沙箱策略层 — 解析 mode / canonicalPath / writableRoots，统一路径可写性判定。

安全设计：所有路径以 ``Path.resolve()`` 归一化后比较，防止符号链接与 ``..``
绕过；``workspace-write`` 仅开放工作区与 ``/tmp``，其余默认只读。
"""
import os
from pathlib import Path

VALID_MODES = {"read-only", "workspace-write", "danger-full-access"}


def _deduplicate_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def canonical_path(p: str) -> str:
    """返回路径的真实规范路径（解析符号链接），失败时回退到原路径。"""
    try:
        return str(Path(p).resolve())
    except (OSError, ValueError, RuntimeError):
        try:
            return os.path.realpath(p)
        except (OSError, ValueError, RuntimeError):
            return p


def resolve_policy(mode: str, workspace_root: str | None = None) -> dict:
    """解析沙箱策略，返回包含 mode/canonicalPath/writableRoots/enforcement 的字典。"""
    if mode not in VALID_MODES:
        raise ValueError(f"invalid mode: {mode}, expected one of {VALID_MODES}")

    policy: dict = {"mode": mode}

    if workspace_root is not None:
        if not isinstance(workspace_root, str) or not workspace_root.strip():
            raise ValueError("workspace_root must be a non-empty string")
        cp = canonical_path(workspace_root)
        if not cp or not cp.strip() or cp == ".":
            raise ValueError("workspace_root resolves to empty path")
        policy["workspaceRoot"] = cp
        policy["canonicalPath"] = cp
        policy["workspace_root"] = cp  # snake alias for convenience
    else:
        if mode == "workspace-write":
            raise ValueError("workspace_root required for workspace-write mode")

    if mode == "workspace-write":
        tmp_canonical = canonical_path("/tmp") if os.path.exists("/tmp") else "/tmp"
        roots = _deduplicate_preserve_order(
            [r for r in [policy.get("workspaceRoot"), tmp_canonical, "/tmp"] if r]
        )
        policy["writableRoots"] = roots
        policy["enforcement"] = "full"
    elif mode == "read-only":
        tmp_canonical = canonical_path("/tmp") if os.path.exists("/tmp") else "/tmp"
        roots = _deduplicate_preserve_order([r for r in [tmp_canonical, "/tmp"] if r])
        policy["writableRoots"] = roots
        policy["enforcement"] = "full"
        if "canonicalPath" not in policy:
            try:
                policy["canonicalPath"] = str(Path.cwd().resolve())
            except (OSError, ValueError, RuntimeError):
                policy["canonicalPath"] = str(Path(".").resolve())
    else:  # danger-full-access
        policy["writableRoots"] = ["/"]  # 全盘可写，仅用于显式危险模式
        policy["enforcement"] = "partial"  # 标记为未强隔离
        if "workspaceRoot" not in policy and workspace_root is None:
            if "canonicalPath" not in policy:
                try:
                    policy["canonicalPath"] = str(Path.cwd().resolve())
                except (OSError, ValueError, RuntimeError):
                    policy["canonicalPath"] = str(Path(".").resolve())

    return policy


def is_path_writable(path: str, policy: dict) -> bool:
    """判断路径是否落在可写根内（规范路径前缀匹配，防穿越）。

    NOTE: This is a TOCTOU-prone check. Caller must not rely on it alone for
    security; open files with O_NOFOLLOW and verify fd path via /proc/self/fd
    or enforce via OS-level sandbox (namespaces).
    """
    cp = canonical_path(path)
    cp_norm = os.path.normcase(cp)
    for r in policy.get("writableRoots", []):
        if r == "/":
            return True  # danger-full-access 全盘可写
        r_norm = os.path.normcase(r)
        try:
            common = os.path.commonpath([cp_norm, r_norm])
        except ValueError:
            continue
        if common == r_norm:
            return True
    return False

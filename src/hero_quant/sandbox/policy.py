"""L1 policy — mode + canonicalPath + writableRoots."""
import os
from pathlib import Path

VALID_MODES = {"read-only", "workspace-write", "danger-full-access"}


def canonical_path(p: str) -> str:
    """Return real canonical path (resolves symlinks)."""
    # realpath is the true source; resolve as fallback
    try:
        return os.path.realpath(p)
    except Exception:
        return str(Path(p).resolve())


def resolve_policy(mode: str, workspace_root: str | None = None) -> dict:
    """
    Resolve sandbox policy.

    - mode: read-only | workspace-write | danger-full-access
    - workspace_root: host path for workspace; canonicalized via realpath
    - writableRoots = {workspaceRoot, /tmp} for workspace-write
    """
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

    # writableRoots per spec
    if mode == "workspace-write":
        roots = []
        if workspace_root is not None:
            roots.append(policy["workspaceRoot"])
        # spec says /tmp is always writable
        tmp_canonical = canonical_path("/tmp") if os.path.exists("/tmp") else "/tmp"
        # ensure /tmp literal is included even on Windows where /tmp may not exist
        if tmp_canonical not in roots:
            roots.append(tmp_canonical)
        if "/tmp" not in roots:
            roots.append("/tmp")
        # dedup preserve order
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
        # dedup
        if len(policy["writableRoots"]) == 2 and policy["writableRoots"][0] == policy["writableRoots"][1]:
            policy["writableRoots"] = ["/tmp"]
        policy["enforcement"] = "full"
    else:  # danger-full-access
        policy["writableRoots"] = ["/"]
        policy["enforcement"] = "partial"
        if "workspaceRoot" not in policy and workspace_root is None:
            # provide a default canonical for consistency
            pass

    return policy


def is_path_writable(path: str, policy: dict) -> bool:
    """Check if path is within writableRoots (canonical prefix check)."""
    cp = canonical_path(path)
    for root in policy.get("writableRoots", []):
        r = canonical_path(root) if root != "/" else "/"
        if r == "/":
            return True
        if cp == r or cp.startswith(r + os.sep):
            return True
        # also allow exact /tmp on Windows where realpath differs
        if root == "/tmp" and (cp == "/tmp" or cp.startswith("/tmp/") or cp.startswith("/tmp\\")):
            return True
    return False

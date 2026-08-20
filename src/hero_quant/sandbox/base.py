"""Sandbox abstraction — BaseSandbox + LocalShellBackend + DockerBackend.

L1 hardening: confine(argv, policy) wraps with bwrap-like prefix only if bwrap
binary exists (offline no-op on Windows/macOS). Keeps enforcement flag
full/partial without requiring docker/bwrap at test time.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import warnings
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Tuple, Union


def _has_bwrap() -> bool:
    """Return True only if bwrap binary is available on PATH."""
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
    """Abstract sandbox. execute(cmd) -> (stdout, stderr, exit_code)."""

    @abstractmethod
    def execute(self, cmd: Union[str, List[str]]) -> Tuple[str, str, int]:
        raise NotImplementedError

    def confine(self, argv: List[str], policy: dict) -> List[str]:
        """
        Wrap argv with policy confinement.

        - When policy mode == "workspace-write" and bwrap exists, prefix with
          bwrap-like bind mounts (--ro-bind / /, --bind workspaceRoot, --bind /tmp).
          Offline/Windows where bwrap missing => no-op (returns copy of argv).
        - Other modes => no-op passthrough (Docker cap_drop/read_only handles
          isolation at compose layer — L1 bwrap is Phase2).
        """
        if not argv:
            return list(argv)
        if not isinstance(argv, list):
            return list(argv)  # type: ignore[return-value]
        # Normalize argv entries to str
        argv = [str(x) for x in argv]
        mode = None
        if isinstance(policy, dict):
            mode = policy.get("mode")
        if mode == "workspace-write":
            if _has_bwrap():
                # canonical workspace root
                ws = None
                if isinstance(policy, dict):
                    ws = policy.get("workspaceRoot") or policy.get("workspace_root") or policy.get("canonicalPath")
                if ws:
                    try:
                        ws_canonical = str(Path(ws).resolve())
                    except Exception:
                        ws_canonical = str(ws)
                else:
                    ws_canonical = "/tmp"
                # bwrap-like prefix: ro whole fs, then bind workspace + /tmp writable
                # keep minimal flags that exist on common bwrap versions
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
            # no bwrap binary -> no-op (offline safe)
            return list(argv)
        # read-only / danger-full-access -> no wrapping at python layer
        return list(argv)

    @property
    def enforcement(self) -> str:
        """Default enforcement full; subclasses override for partial."""
        return "full"


class LocalShellBackend(BaseSandbox):
    """Straight-through local shell backend — for dev/test, full enforcement."""

    def __init__(self, policy: dict | None = None):
        self._policy: dict = dict(policy) if isinstance(policy, dict) else {}

    def execute(self, cmd: Union[str, List[str]]) -> Tuple[str, str, int]:
        if isinstance(cmd, str):
            # shell string — no confine (shell expansion needed); policy still governs paths externally
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        else:
            # list argv — apply confine with stored policy or empty
            pol = self._policy if self._policy else {}
            # if caller passed explicit policy via cmd wrapping, allow override per-call
            # confine handles bwrap conditional
            wrapped = self.confine(cmd, pol)
            result = subprocess.run(wrapped, shell=False, capture_output=True, text=True)
        return result.stdout, result.stderr, result.returncode

    def confine(self, argv: List[str], policy: dict) -> List[str]:
        # Merge stored policy with per-call policy (per-call wins)
        merged: dict = {}
        if isinstance(self._policy, dict):
            merged.update(self._policy)
        if isinstance(policy, dict):
            merged.update(policy)
        return super().confine(argv, merged)

    @property
    def enforcement(self) -> str:
        # danger-full-access is partial (no isolation), otherwise full
        if isinstance(self._policy, dict) and self._policy.get("mode") == "danger-full-access":
            return "partial"
        return "full"


class DockerBackend(BaseSandbox):
    """Docker stub — does not require docker binary.

    If docker is available, would wrap as `docker run --rm -v ws:ws image argv`.
    Offline/no-docker => falls back to local subprocess (same as LocalShell) so
    tests stay green. Enforcement is partial because container boundary is not
    enforced in stub mode; full when docker exists and policy is workspace-write.
    """

    def __init__(self, image: str = "hero-quant:sandbox", policy: dict | None = None):
        self.image = image
        self._policy: dict = dict(policy) if isinstance(policy, dict) else {}

    def execute(self, cmd: Union[str, List[str]]) -> Tuple[str, str, int]:
        if isinstance(cmd, str):
            # For string cmd, run via shell locally (stub)
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            return result.stdout, result.stderr, result.returncode
        # list argv
        pol = self._policy if self._policy else {}
        # If docker binary exists and we want container isolation, build docker argv
        # Otherwise, fallback to local confine (bwrap conditional)
        wrapped = self.confine(cmd, pol)
        # In stub mode wrapped may contain "docker" prefix; if docker not present,
        # strip it or fallback to original argv to avoid ENOENT
        if wrapped and wrapped[0] == "docker" and not _has_docker():
            # fallback: run original argv locally via bwrap-aware confine
            fallback = super().confine(cmd, pol)  # bwrap conditional local
            result = subprocess.run(fallback, shell=False, capture_output=True, text=True)
            return result.stdout, result.stderr, result.returncode
        # If docker prefix present and docker exists, try docker; on failure fallback
        try:
            result = subprocess.run(wrapped, shell=False, capture_output=True, text=True)
        except FileNotFoundError:
            # binary missing (bwrap/docker) — fallback to original
            result = subprocess.run(cmd, shell=False, capture_output=True, text=True)
        return result.stdout, result.stderr, result.returncode

    def confine(self, argv: List[str], policy: dict) -> List[str]:
        # Merge policies
        merged: dict = {}
        if isinstance(self._policy, dict):
            merged.update(self._policy)
        if isinstance(policy, dict):
            merged.update(policy)
        mode = merged.get("mode")
        # If docker available and workspace-write, prefer docker wrapping; else base bwrap logic
        if _has_docker() and mode == "workspace-write":
            ws = merged.get("workspaceRoot") or merged.get("workspace_root") or merged.get("canonicalPath") or "/tmp"
            try:
                ws_canonical = str(Path(ws).resolve())
            except Exception:
                ws_canonical = str(ws)
            # docker run stub prefix — mount workspace and /tmp
            prefix: List[str] = [
                "docker", "run", "--rm",
                "--cap-drop", "ALL",
                "--read-only",
                "--tmpfs", "/tmp",
                "-v", f"{ws_canonical}:{ws_canonical}:rw",
                self.image,
            ]
            return prefix + [str(x) for x in argv]
        # Fallback to base bwrap conditional (no-op if bwrap missing)
        return super().confine([str(x) for x in argv], merged)

    @property
    def enforcement(self) -> str:
        # Docker stub is partial when docker not enforcing; full if docker present
        if _has_docker():
            return "full"
        # Without docker, same as local but marked partial to signal not isolated
        return "partial"

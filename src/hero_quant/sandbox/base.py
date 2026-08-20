"""Sandbox abstraction — BaseSandbox + LocalShellBackend."""
from abc import ABC, abstractmethod
import subprocess
import shlex
from typing import Tuple, List, Union


class BaseSandbox(ABC):
    """Abstract sandbox. execute(cmd) -> (stdout, stderr, exit_code)."""

    @abstractmethod
    def execute(self, cmd: Union[str, List[str]]) -> Tuple[str, str, int]:
        raise NotImplementedError

    def confine(self, argv: List[str], policy: dict) -> List[str]:
        """
        Wrap argv with policy confinement.
        Placeholder: returns argv unchanged (L1 bwrap/nsjail will override).
        """
        return argv

    @property
    def enforcement(self) -> str:
        return "full"


class LocalShellBackend(BaseSandbox):
    """Straight-through local shell backend (no isolation) — for dev/test."""

    def execute(self, cmd: Union[str, List[str]]) -> Tuple[str, str, int]:
        if isinstance(cmd, str):
            # run via shell
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        else:
            # list argv — no shell
            wrapped = self.confine(cmd, {})
            result = subprocess.run(wrapped, shell=False, capture_output=True, text=True)
        return result.stdout, result.stderr, result.returncode

    def confine(self, argv: List[str], policy: dict) -> List[str]:
        # L1 placeholder: no wrapping yet, return as-is
        # Future: bwrap / nsjail wrapping based on policy
        return argv

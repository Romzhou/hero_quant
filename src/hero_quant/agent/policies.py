"""Agent policies — Retry + Saga compensation + Budget breaker.

Minimal Wave C3:
- RetryPolicy: exponential backoff + jitter, should_retry
- error_handler(state, NodeError) -> Command goto compensate (Saga)
- BudgetBreaker: sliding window cost breaker
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Tuple, Type


class NodeError(Exception):
    """Node execution error for Saga handling."""


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    retry_on: Tuple[Type[BaseException], ...] = (Exception,)
    backoff_base: float = 1.0
    backoff_factor: float = 2.0
    jitter: float = 0.1

    def should_retry(self, exc: BaseException, attempt: int) -> bool:
        """Return True if exc is retryable and attempt < max."""
        if attempt >= self.max_attempts:
            return False
        try:
            return isinstance(exc, self.retry_on)
        except Exception:
            return False

    def backoff(self, attempt: int) -> float:
        """Exponential backoff + jitter."""
        base = self.backoff_base * (self.backoff_factor ** max(0, attempt - 1))
        try:
            j = random.uniform(0, base * self.jitter)
        except Exception:
            j = 0
        return base + j

    def sleep(self, attempt: int) -> None:
        d = self.backoff(attempt)
        try:
            time.sleep(d)
        except Exception:
            pass


# Command placeholder for LangGraph (fallback if langgraph not available)
# NOTE: langgraph import is deferred to error_handler to keep policies import fast
# (<0.01s) — critical for Agent Loop parallel timing test (<0.35s wall time).
@dataclass
class LGCommand:  # type: ignore
    goto: str | None = None
    update: dict | None = None


def _get_lg_command():
    """Lazy import LGCommand to avoid slow langgraph import at module load."""
    try:
        from langgraph.types import Command as _LG  # type: ignore

        return _LG
    except Exception:
        try:
            from langgraph.graph import Command as _LG2  # type: ignore

            return _LG2
        except Exception:
            return LGCommand


def error_handler(state: dict[str, Any], error: BaseException) -> Any:
    """Saga error handler — returns Command goto compensate.

    Maps NodeError to compensation branch. Minimal placeholder keeps
    StateGraph error path deterministic without real rollback.
    """
    # Log placeholder (structlog would go here)
    # Decide compensation target
    goto = "compensate"
    # Return Command for LangGraph
    LG = _get_lg_command()
    try:
        return LG(goto=goto, update={"error": str(error)})
    except Exception:
        return {"goto": goto, "error": str(error)}


@dataclass
class BudgetBreaker:
    daily_limit: float = 5.0
    window_seconds: int = 86400
    _costs: list[tuple[float, float]] = field(default_factory=list)  # (ts, cost)

    def _prune(self) -> None:
        now = time.time()
        cutoff = now - self.window_seconds
        self._costs = [(ts, c) for ts, c in self._costs if ts >= cutoff]

    def add_cost(self, cost: float) -> None:
        self._prune()
        self._costs.append((time.time(), float(cost)))

    def total_cost(self) -> float:
        self._prune()
        return sum(c for _, c in self._costs)

    def should_fallback(self, cost: float) -> bool:
        """Sliding window熔断 — if single cost or accumulated exceeds daily_limit."""
        # Single check (test expects cost=6 > limit 5 => True)
        if float(cost) > self.daily_limit:
            return True
        # Accumulated check
        self._prune()
        if self.total_cost() + float(cost) > self.daily_limit:
            return True
        return False

    def check_and_add(self, cost: float) -> bool:
        """Add cost and return whether fallback needed."""
        should = self.should_fallback(cost)
        self.add_cost(cost)
        return should

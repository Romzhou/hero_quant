"""Wall-time governance — budget enforcement + observability.

Wave E follow-up after E1 temporal heartbeat/circuit:
- WallTimeExceeded: hard error when budget exceeded
- WallTimeBudget: monotonic wall-time budget (budget_seconds, elapsed, remaining, check)
- WallTimeGovernor: wraps callables / context manager with metrics hardening
- with_wall_time_budget: decorator / context manager factory
- enforce_wall_time: helper for sync call timing

Metrics hardening:
- hero_quant_wall_time_seconds histogram (operation/status) observed on every exit
- hero_quant_governance_wall_time_exceeded_total counter incremented on exceed
- Works offline (no prometheus_client required) -> no-op metrics fallback

Budget source:
- Explicit budget_seconds param
- Env HERO_WALL_TIME_BUDGET (seconds) via Settings or os.environ
- Default: 30.0s if not specified (conservative)

Usage:
    from hero_quant.governance.wall_time import WallTimeBudget, WallTimeGovernor, with_wall_time_budget

    with WallTimeBudget(budget_seconds=0.5, operation="backtest") as b:
        heavy_work()
        b.check()  # raises WallTimeExceeded if > budget

    @with_wall_time_budget(1.0, operation="ledger_append")
    def do_append(...): ...

    governor = WallTimeGovernor(budget_seconds=2.0, operation="agent_loop")
    result = governor.enforce(lambda: loop.run(goal))

Wall-time budget enforced via monotonic clock (time.monotonic), not wall clock.
"""

from __future__ import annotations

import os
import time
import functools
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# metrics integration (optional, offline-safe)
try:
    from hero_quant.metrics import observe_wall_time as _observe_wall_time
    from hero_quant.metrics import inc_wall_time_exceeded as _inc_exceeded
except Exception:
    _observe_wall_time = None  # type: ignore
    _inc_exceeded = None  # type: ignore


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class WallTimeExceeded(RuntimeError):
    """Raised when wall-time budget is exceeded."""

    def __init__(self, operation: str, budget_seconds: float, elapsed: float, detail: str | None = None):
        msg = f"wall-time budget exceeded for {operation!r}: budget={budget_seconds:.4f}s elapsed={elapsed:.4f}s"
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)
        self.operation = operation
        self.budget_seconds = float(budget_seconds)
        self.elapsed = float(elapsed)
        self.detail = detail


class WallTimeBudgetExceeded(WallTimeExceeded):
    """Alias for WallTimeExceeded (compat)."""


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------
_DEFAULT_BUDGET_SECONDS: float = 30.0


def _resolve_default_budget(explicit: float | None = None) -> float | None:
    if explicit is not None:
        try:
            v = float(explicit)
            if v <= 0:
                return None  # 0 or negative means unlimited
            return v
        except Exception:
            return None
    # env fallback
    raw = os.environ.get("HERO_WALL_TIME_BUDGET", os.environ.get("HERO_WALL_TIME_BUDGET_SECONDS", ""))
    if raw and str(raw).strip():
        try:
            v = float(str(raw).strip())
            if v > 0:
                return v
        except Exception:
            pass
    # try Settings if available
    try:
        from hero_quant.config.settings import Settings  # type: ignore

        s = Settings()
        # settings may have wall_time_budget attr
        val = getattr(s, "wall_time_budget_seconds", None) or getattr(s, "wall_time_budget", None)
        if val is not None:
            try:
                v = float(val)
                if v > 0:
                    return v
            except Exception:
                pass
    except Exception:
        pass
    return float(_DEFAULT_BUDGET_SECONDS)


@dataclass
class WallTimeBudget:
    """Monotonic wall-time budget.

    Args:
        budget_seconds: max wall-time allowed; None or <=0 means unlimited
        operation: label for metrics
        start_time: monotonic start (auto)
        deadline: monotonic deadline (start + budget)

    Provides:
        elapsed() -> float
        remaining() -> float | None (None if unlimited)
        exceeded() -> bool
        check() -> raises WallTimeExceeded if exceeded
        __enter__/__exit__ -> context manager with auto check + metrics
    """

    budget_seconds: float | None = field(default_factory=lambda: _resolve_default_budget())
    operation: str = "generic"
    _start: float = field(default_factory=time.monotonic, init=False, repr=False)
    _deadline: float | None = field(default=None, init=False, repr=False)
    _exceeded_recorded: bool = field(default=False, init=False, repr=False)

    def __post_init__(self):
        # normalize budget
        if self.budget_seconds is not None:
            try:
                b = float(self.budget_seconds)
                if b <= 0:
                    self.budget_seconds = None
                else:
                    self.budget_seconds = b
            except Exception:
                self.budget_seconds = None
        # compute deadline
        if self.budget_seconds is not None:
            try:
                self._deadline = float(self._start) + float(self.budget_seconds)
            except Exception:
                self._deadline = None
        else:
            self._deadline = None

    # -- timing helpers --
    def elapsed(self) -> float:
        try:
            return float(time.monotonic() - float(self._start))
        except Exception:
            return 0.0

    def remaining(self) -> float | None:
        if self.budget_seconds is None or self._deadline is None:
            return None
        try:
            rem = float(self._deadline) - float(time.monotonic())
            return float(rem)
        except Exception:
            return None

    def exceeded(self) -> bool:
        if self.budget_seconds is None or self._deadline is None:
            return False
        try:
            return float(time.monotonic()) >= float(self._deadline)
        except Exception:
            return False

    def is_exceeded(self) -> bool:
        return self.exceeded()

    def check(self, detail: str | None = None) -> None:
        """Raise WallTimeExceeded if budget exceeded, else no-op."""
        if self.exceeded():
            el = self.elapsed()
            # metrics hardening: increment exceeded counter (once)
            try:
                if _inc_exceeded is not None and not self._exceeded_recorded:
                    _inc_exceeded(self.operation)
                    self._exceeded_recorded = True
            except Exception:
                pass
            # observe wall-time with status exceeded
            try:
                if _observe_wall_time is not None:
                    _observe_wall_time(self.operation, el, status="exceeded")
            except Exception:
                pass
            raise WallTimeExceeded(self.operation, float(self.budget_seconds or 0), float(el), detail=detail)

    def enforce(self) -> None:
        """Alias for check()."""
        self.check()

    # -- context manager --
    def __enter__(self) -> "WallTimeBudget":
        # reset start to entry time for precise measurement within context
        self._start = time.monotonic()
        if self.budget_seconds is not None:
            self._deadline = float(self._start) + float(self.budget_seconds)
        else:
            self._deadline = None
        self._exceeded_recorded = False
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        elapsed = self.elapsed()
        status = "success"
        # if exception is WallTimeExceeded, mark status exceeded
        if exc_type is not None and issubclass(exc_type, WallTimeExceeded):
            status = "exceeded"
            try:
                if _inc_exceeded is not None and not self._exceeded_recorded:
                    _inc_exceeded(self.operation)
                    self._exceeded_recorded = True
            except Exception:
                pass
        elif self.exceeded():
            status = "exceeded"
            # auto-check raises; but in __exit__ we want to record and optionally raise if no prior exception
            if exc_type is None:
                # no prior exception, but budget exceeded -> raise
                try:
                    if _inc_exceeded is not None and not self._exceeded_recorded:
                        _inc_exceeded(self.operation)
                except Exception:
                    pass
                try:
                    if _observe_wall_time is not None:
                        _observe_wall_time(self.operation, elapsed, status=status)
                except Exception:
                    pass
                # raise after metrics
                raise WallTimeExceeded(self.operation, float(self.budget_seconds or 0), float(elapsed))
        # observe wall-time
        try:
            if _observe_wall_time is not None:
                _observe_wall_time(self.operation, elapsed, status=status)
        except Exception:
            pass
        # do not suppress exceptions
        return False

    # -- helper: timed decorator compatibility --
    def time_call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Time a callable within budget, raising if exceeded after call."""
        start = time.monotonic()
        try:
            result = fn(*args, **kwargs)
            return result
        finally:
            dur = time.monotonic() - start
            status = "success"
            try:
                if self.budget_seconds is not None and dur > float(self.budget_seconds):
                    status = "exceeded"
                    if _inc_exceeded is not None:
                        _inc_exceeded(self.operation)
                    raise WallTimeExceeded(self.operation, float(self.budget_seconds), float(dur))
            finally:
                try:
                    if _observe_wall_time is not None:
                        _observe_wall_time(self.operation, dur, status=status)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Governor (higher-level wrapper with enforce callable + metrics)
# ---------------------------------------------------------------------------
class WallTimeGovernor:
    """Governor that enforces wall-time budget around arbitrary callables.

    Provides:
        enforce(fn, *args, **kwargs) -> result or raises WallTimeExceeded
        wrap(fn) -> wrapped function with budget

    Metrics: observes wall-time histogram and increments exceeded counter.
    """

    def __init__(self, budget_seconds: float | None = None, operation: str = "generic", clock: Any = None):
        self.budget_seconds = _resolve_default_budget(budget_seconds) if budget_seconds is not None else _resolve_default_budget()
        # handle 0/negative as unlimited -> None
        if self.budget_seconds is not None:
            try:
                if float(self.budget_seconds) <= 0:
                    self.budget_seconds = None
            except Exception:
                self.budget_seconds = None
        self.operation = str(operation)
        self.clock = clock  # not used now, placeholder for injectable clock in tests

    def _budget(self) -> WallTimeBudget:
        return WallTimeBudget(budget_seconds=self.budget_seconds, operation=self.operation)

    def enforce(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Execute fn within wall-time budget; raise WallTimeExceeded if budget exceeded."""
        budget = self._budget()
        start = time.monotonic()
        status = "success"
        try:
            # Use context for auto-check after call; but we also need pre-check elapsed
            # Simple: run fn, then check elapsed vs budget
            result = fn(*args, **kwargs)
            elapsed = time.monotonic() - start
            if budget.budget_seconds is not None and elapsed > float(budget.budget_seconds):
                status = "exceeded"
                try:
                    if _inc_exceeded is not None:
                        _inc_exceeded(self.operation)
                except Exception:
                    pass
                try:
                    if _observe_wall_time is not None:
                        _observe_wall_time(self.operation, elapsed, status=status)
                except Exception:
                    pass
                raise WallTimeExceeded(self.operation, float(budget.budget_seconds), float(elapsed))
            return result
        except WallTimeExceeded:
            raise
        except Exception:
            # observe wall-time even on error but don't count as exceeded unless duration > budget
            elapsed = time.monotonic() - start
            # if duration > budget, mark exceeded
            if budget.budget_seconds is not None and elapsed > float(budget.budget_seconds):
                status = "exceeded"
                try:
                    if _inc_exceeded is not None:
                        _inc_exceeded(self.operation)
                except Exception:
                    pass
            try:
                if _observe_wall_time is not None:
                    _observe_wall_time(self.operation, elapsed, status=status)
            except Exception:
                pass
            raise
        finally:
            # ensure observe if not already (success path observed above for exceeded case,
            # but success path needs observe)
            if status == "success":
                try:
                    elapsed = time.monotonic() - start
                    if _observe_wall_time is not None:
                        # avoid double observe for exceeded paths handled above
                        # For success, observe now if not already observed (we observed only in exceeded branch)
                        # So observe success here if budget not exceeded
                        if not (budget.budget_seconds is not None and elapsed > float(budget.budget_seconds)):
                            _observe_wall_time(self.operation, elapsed, status="success")
                except Exception:
                    pass

    def wrap(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Return wrapped function that enforces budget."""
        gov = self

        @functools.wraps(fn)
        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            return gov.enforce(fn, *args, **kwargs)

        return _wrapped

    def check_budget(self, elapsed: float) -> None:
        """Check elapsed against budget and raise if exceeded."""
        if self.budget_seconds is None:
            return
        if float(elapsed) > float(self.budget_seconds):
            try:
                if _inc_exceeded is not None:
                    _inc_exceeded(self.operation)
            except Exception:
                pass
            try:
                if _observe_wall_time is not None:
                    _observe_wall_time(self.operation, float(elapsed), status="exceeded")
            except Exception:
                pass
            raise WallTimeExceeded(self.operation, float(self.budget_seconds), float(elapsed))


# ---------------------------------------------------------------------------
# Helpers / decorators
# ---------------------------------------------------------------------------
def with_wall_time_budget(
    budget_seconds: float | None = None,
    operation: str = "generic",
    *,
    budget: float | None = None,
    wall_time_budget: float | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator factory to enforce wall-time budget.

    Supports aliases: budget, wall_time_budget for compat.
    Usage:
        @with_wall_time_budget(0.5)
        def foo(): ...

        @with_wall_time_budget(budget_seconds=1.0, operation="backtest")
        def bar(): ...

    If decorated function exceeds budget, WallTimeExceeded is raised.
    Metrics hardened: observes wall-time and increments exceeded counter.
    """
    # alias handling
    if budget is not None and budget_seconds is None:
        budget_seconds = budget
    if wall_time_budget is not None and budget_seconds is None:
        budget_seconds = wall_time_budget

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        gov = WallTimeGovernor(budget_seconds=budget_seconds, operation=operation)

        @functools.wraps(fn)
        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            return gov.enforce(fn, *args, **kwargs)

        # expose budget for inspection
        _wrapped._wall_time_budget = budget_seconds  # type: ignore[attr-defined]
        _wrapped._wall_time_operation = operation  # type: ignore[attr-defined]
        return _wrapped

    return decorator


def enforce_wall_time(
    fn: Callable[..., Any],
    *args: Any,
    budget_seconds: float | None = None,
    operation: str = "generic",
    **kwargs: Any,
) -> Any:
    """One-shot helper: enforce wall-time budget around a call.

    Args:
        fn: callable to invoke
        *args, **kwargs: passed to fn
        budget_seconds: budget in seconds (uses env/default if None)
        operation: metric label
    Returns:
        fn(*args, **kwargs) result
    Raises:
        WallTimeExceeded if elapsed > budget_seconds
    """
    gov = WallTimeGovernor(budget_seconds=budget_seconds, operation=operation)
    return gov.enforce(fn, *args, **kwargs)


def wall_time_budget(
    budget_seconds: float | None = None,
    operation: str = "generic",
) -> WallTimeBudget:
    """Factory for context manager: `with wall_time_budget(0.5): ...`"""
    return WallTimeBudget(budget_seconds=budget_seconds, operation=operation)


# Convenience aliases for compat with potential test imports
WallTimeBudgetEnforcer = WallTimeGovernor
BudgetEnforcer = WallTimeGovernor
GovernanceWallTimeBudget = WallTimeBudget

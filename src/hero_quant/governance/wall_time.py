"""wall_time — 壁钟时间预算治理。

职责：以 monotonic 时钟度量 wall-time budget，对超时操作抛 WallTimeExceeded 并上报可观测指标。
架构位置：治理层横切能力，被回测、账本追加、对账等耗时路径复用。
关键设计：预算扣减模型为 deadline = start + budget_seconds，elapsed/remaining/exceeded 均基于 time.monotonic，避免 wall clock 回拨影响；预算来源优先级为显式参数 > 环境变量 HERO_WALL_TIME_BUDGET > Settings > 默认 30s；每次退出统一 observe_wall_time，超时递增 exceeded 计数，离线时静默降级。
"""

from __future__ import annotations

import os
import time
import functools
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# 指标集成（可选、离线安全）：缺失时退化为 no-op，避免硬依赖
try:
    from hero_quant.metrics import observe_wall_time as _observe_wall_time
    from hero_quant.metrics import inc_wall_time_exceeded as _inc_exceeded
except Exception:
    _observe_wall_time = None  # type: ignore
    _inc_exceeded = None  # type: ignore


# 异常
class WallTimeExceeded(RuntimeError):
    """壁钟预算耗尽时抛出，携带 operation/budget/elapsed 以便上层决策重试或降级。"""

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
    """WallTimeExceeded 别名，兼容旧导入路径。"""


# 预算解析与数据结构
_DEFAULT_BUDGET_SECONDS: float = 30.0


def _resolve_default_budget(explicit: float | None = None) -> float | None:
    """按 显式参数 > 环境变量 > Settings > 默认值 解析预算；0 或负数视为不限。"""
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
    """基于 monotonic 的壁钟预算；支持上下文管理与超时自动校验。

    不变量：deadline = start + budget_seconds；remaining 可能为负表示已超时；check/exceeded 均以 monotonic 为准。
    """

    budget_seconds: float | None = field(default_factory=lambda: _resolve_default_budget())
    operation: str = "generic"
    _start: float = field(default_factory=time.monotonic, init=False, repr=False)
    _deadline: float | None = field(default=None, init=False, repr=False)
    _exceeded_recorded: bool = field(default=False, init=False, repr=False)

    def __post_init__(self):
        # 归一化预算并计算 deadline，0/负数按不限处理
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

    # 计时辅助：均以 monotonic 为时间源，避免 wall clock 跳变
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
        """若已超时则抛 WallTimeExceeded 并记录 exceeded 指标，否则静默。"""
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
        """check 的别名。"""

        self.check()

    # 上下文管理：进入时重置起点以精确度量 with 块内耗时
    def __enter__(self) -> "WallTimeBudget":
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

    def time_call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """在预算内执行可调用对象，超时则抛 WallTimeExceeded 并上报耗时。"""
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


# 上层 Governor：封装任意可调用对象的预算执行与指标上报
class WallTimeGovernor:
    """壁钟预算执行器，围绕任意可调用对象强制超时并统一度量。"""

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
        """在预算内执行 fn，超时抛 WallTimeExceeded；始终上报 wall-time 直方图。"""
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
        """返回带预算约束的包装函数。"""
        gov = self

        @functools.wraps(fn)
        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            return gov.enforce(fn, *args, **kwargs)

        return _wrapped

    def check_budget(self, elapsed: float) -> None:
        """校验已耗 elapsed 是否超预算，超则抛异常并计数。"""
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


# 快捷装饰器与辅助
def with_wall_time_budget(
    budget_seconds: float | None = None,
    operation: str = "generic",
    *,
    budget: float | None = None,
    wall_time_budget: float | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """装饰器工厂：为函数附加壁钟预算，超时抛 WallTimeExceeded。

    兼容 budget / wall_time_budget 别名参数。
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
    """一次性辅助：围绕单次调用强制壁钟预算。"""
    gov = WallTimeGovernor(budget_seconds=budget_seconds, operation=operation)
    return gov.enforce(fn, *args, **kwargs)


def wall_time_budget(
    budget_seconds: float | None = None,
    operation: str = "generic",
) -> WallTimeBudget:
    """上下文管理器工厂：`with wall_time_budget(0.5): ...`。"""
    return WallTimeBudget(budget_seconds=budget_seconds, operation=operation)


# 兼容别名：保留旧导入路径
WallTimeBudgetEnforcer = WallTimeGovernor
BudgetEnforcer = WallTimeGovernor
GovernanceWallTimeBudget = WallTimeBudget

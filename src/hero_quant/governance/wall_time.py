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
from typing import Any, Callable

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
        except (ValueError, TypeError) as e:
            raise ValueError(f"invalid wall-time budget: {explicit!r}") from e
    # env fallback
    raw = os.environ.get("HERO_WALL_TIME_BUDGET", os.environ.get("HERO_WALL_TIME_BUDGET_SECONDS", ""))
    if raw and str(raw).strip():
        try:
            v = float(str(raw).strip())
            if v > 0:
                return v
        except (ValueError, TypeError):
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
            except (ValueError, TypeError):
                pass
    except (ImportError, OSError, ValueError, TypeError):
        pass
    return float(_DEFAULT_BUDGET_SECONDS)


@dataclass
class WallTimeBudget:
    """基于 monotonic 的壁钟预算；支持上下文管理与超时自动校验。

    不变量：deadline = start + budget_seconds；remaining 可能为负表示已超时；check/exceeded 均以 monotonic 为准。
    """

    budget_seconds: float | None = field(default_factory=lambda: _resolve_default_budget())
    operation: str = "generic"
    clock: Any | None = field(default=None, repr=False, compare=False)
    _start: float = field(default_factory=time.monotonic, init=False, repr=False)
    _deadline: float | None = field(default=None, init=False, repr=False)
    _exceeded_recorded: bool = field(default=False, init=False, repr=False)
    _now: Callable[[], float] = field(default=None, init=False, repr=False)  # type: ignore[assignment]

    def __post_init__(self):
        # wire clock via self._now
        if self.clock is not None:
            if callable(self.clock):
                self._now = self.clock  # type: ignore[assignment]
            elif hasattr(self.clock, "monotonic") and callable(getattr(self.clock, "monotonic")):
                self._now = getattr(self.clock, "monotonic")  # type: ignore[assignment]
            else:
                self._now = time.monotonic  # type: ignore[assignment]
            try:
                self._start = float(self._now())  # type: ignore[operator]
            except Exception:
                self._start = time.monotonic()
        else:
            self._now = time.monotonic  # type: ignore[assignment]
        # 归一化预算并计算 deadline，0/负数按不限处理；非法值 raise ValueError
        if self.budget_seconds is not None:
            try:
                b = float(self.budget_seconds)  # type: ignore[arg-type]
                if b <= 0:
                    self.budget_seconds = None
                else:
                    self.budget_seconds = b
            except (ValueError, TypeError) as e:
                raise ValueError(f"invalid wall-time budget: {self.budget_seconds!r}") from e
        # compute deadline
        if self.budget_seconds is not None:
            try:
                self._deadline = float(self._start) + float(self.budget_seconds)
            except (ValueError, TypeError):
                self._deadline = None
        else:
            self._deadline = None

    # 计时辅助：均以 monotonic 为时间源，避免 wall clock 跳变
    def elapsed(self) -> float:
        try:
            return float(self._now() - float(self._start))  # type: ignore[operator]
        except Exception:
            return 0.0

    def remaining(self) -> float | None:
        if self.budget_seconds is None or self._deadline is None:
            return None
        try:
            rem = float(self._deadline) - float(self._now())  # type: ignore[operator]
            return float(rem)
        except Exception:
            return None

    def exceeded(self) -> bool:
        if self.budget_seconds is None or self._deadline is None:
            return False
        try:
            return float(self._now()) >= float(self._deadline)  # type: ignore[operator]
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
        self._start = self._now()  # type: ignore[operator]
        if self.budget_seconds is not None:
            self._deadline = float(self._start) + float(self.budget_seconds)
        else:
            self._deadline = None
        self._exceeded_recorded = False
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        elapsed = self.elapsed()
        status = "success"
        exceeded = False
        # 统一 status 计算
        try:
            if exc_type is not None and issubclass(exc_type, WallTimeExceeded):
                status = "exceeded"
                exceeded = True
            elif self.exceeded():
                status = "exceeded"
                exceeded = True
        except Exception:
            # issubclass may raise TypeError for non-class exc_type
            if self.exceeded():
                status = "exceeded"
                exceeded = True
        # 计数一次
        if exceeded and not self._exceeded_recorded:
            try:
                if _inc_exceeded is not None:
                    _inc_exceeded(self.operation)
                    self._exceeded_recorded = True
            except Exception:
                pass
        # 单次 observe
        try:
            if _observe_wall_time is not None:
                _observe_wall_time(self.operation, elapsed, status=status)
        except Exception:
            pass
        # 超时且无原异常时才 raise，不吞原异常
        if exceeded and exc_type is None:
            raise WallTimeExceeded(self.operation, float(self.budget_seconds or 0), float(elapsed))
        # do not suppress exceptions
        return False

    def time_call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """在预算内执行可调用对象，超时则抛 WallTimeExceeded 并上报耗时。"""
        start = self._now()  # type: ignore[operator]
        try:
            result = fn(*args, **kwargs)
        except Exception:
            dur = self._now() - start  # type: ignore[operator]
            # 单次 observe，不吞原异常
            try:
                if _observe_wall_time is not None:
                    status = "exceeded" if self.budget_seconds is not None and dur > float(self.budget_seconds) else "success"
                    _observe_wall_time(self.operation, dur, status=status)
            except Exception:
                pass
            raise
        else:
            dur = self._now() - start  # type: ignore[operator]
            if self.budget_seconds is not None and dur > float(self.budget_seconds):
                try:
                    if _inc_exceeded is not None:
                        _inc_exceeded(self.operation)
                except Exception:
                    pass
                try:
                    if _observe_wall_time is not None:
                        _observe_wall_time(self.operation, dur, status="exceeded")
                except Exception:
                    pass
                raise WallTimeExceeded(self.operation, float(self.budget_seconds), float(dur))
            try:
                if _observe_wall_time is not None:
                    _observe_wall_time(self.operation, dur, status="success")
            except Exception:
                pass
            return result


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
            except (ValueError, TypeError) as e:
                raise ValueError(f"invalid wall-time budget: {self.budget_seconds!r}") from e
        self.operation = str(operation)
        self.clock = clock
        # wire clock via self._now
        if clock is not None:
            if callable(clock):
                self._now = clock  # type: ignore[attr-defined]
            elif hasattr(clock, "monotonic") and callable(getattr(clock, "monotonic")):
                self._now = getattr(clock, "monotonic")  # type: ignore[attr-defined]
            else:
                self._now = time.monotonic  # type: ignore[attr-defined]
        else:
            self._now = time.monotonic  # type: ignore[attr-defined]

    def _budget(self) -> WallTimeBudget:
        return WallTimeBudget(budget_seconds=self.budget_seconds, operation=self.operation, clock=self.clock)

    def enforce(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """在预算内执行 fn，超时抛 WallTimeExceeded；始终上报 wall-time 直方图。"""
        budget = self._budget()
        start = self._now()  # type: ignore[operator]
        status = "success"
        elapsed: float = 0.0
        try:
            result = fn(*args, **kwargs)
            elapsed = self._now() - start  # type: ignore[operator]
            if budget.budget_seconds is not None and elapsed > float(budget.budget_seconds):
                status = "exceeded"
                try:
                    if _inc_exceeded is not None:
                        _inc_exceeded(self.operation)
                except Exception:
                    pass
                raise WallTimeExceeded(self.operation, float(budget.budget_seconds), float(elapsed))
            return result
        except WallTimeExceeded:
            # 保证 elapsed 已计算
            if elapsed == 0.0:
                try:
                    elapsed = self._now() - start  # type: ignore[operator]
                except Exception:
                    elapsed = 0.0
            status = "exceeded"
            raise
        except Exception:
            try:
                elapsed = self._now() - start  # type: ignore[operator]
            except Exception:
                elapsed = 0.0
            if budget.budget_seconds is not None and elapsed > float(budget.budget_seconds):
                status = "exceeded"
                try:
                    if _inc_exceeded is not None:
                        _inc_exceeded(self.operation)
                except Exception:
                    pass
            raise
        finally:
            # 单 finally observe，去双计
            try:
                if _observe_wall_time is not None:
                    # elapsed 可能在成功路径已算，异常路径也已算；兜底再算一次
                    if elapsed == 0.0:
                        try:
                            elapsed = self._now() - start  # type: ignore[operator]
                        except Exception:
                            elapsed = 0.0
                    _observe_wall_time(self.operation, elapsed, status=status)
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

"""Agent 策略：重试、Saga 补偿与预算熔断。

职责：为研究团队图与 Agent 循环提供容错与成本控制原语。
架构位置：agent 层策略组件，被 graph/loop 按需调用，保持轻量无重依赖。
关键设计：
- RetryPolicy：指数退避 + 抖动，可判定 should_retry 并提供 sleep
- error_handler：Saga 补偿入口，映射 NodeError 到 compensate 分支（Command）
- BudgetBreaker：滑动窗口成本熔断，单次或累计超阈即触发降级
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Tuple, Type


class NodeError(Exception):
    """节点执行异常，用于触发 Saga 补偿分支."""


@dataclass
class RetryPolicy:
    """指数退避重试策略，支持抖动与类型过滤."""

    max_attempts: int = 3
    retry_on: Tuple[Type[BaseException], ...] = (Exception,)
    backoff_base: float = 1.0
    backoff_factor: float = 2.0
    jitter: float = 0.1

    def should_retry(self, exc: BaseException, attempt: int) -> bool:
        """判断是否可重试：未超次数且异常类型匹配."""
        if attempt >= self.max_attempts:
            return False
        try:
            return isinstance(exc, self.retry_on)
        except Exception:
            return False

    def backoff(self, attempt: int) -> float:
        """计算指数退避时长，附加随机抖动."""
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


@dataclass
class LGCommand:  # type: ignore
    """LangGraph Command 占位，缺失时用于回落."""

    goto: str | None = None
    update: dict | None = None


def _get_lg_command():
    """懒加载 LangGraph Command，避免导入耗时影响启动."""
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
    """Saga 补偿入口，返回指向 compensate 的 Command，占位实现确定性回退."""
    goto = "compensate"
    LG = _get_lg_command()
    try:
        return LG(goto=goto, update={"error": str(error)})
    except Exception:
        return {"goto": goto, "error": str(error)}


@dataclass
class BudgetBreaker:
    """滑动窗口预算熔断器，按日限额判定是否降级."""

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
        """滑动窗口熔断：单次或累计超阈即需降级."""
        if float(cost) > self.daily_limit:
            return True
        self._prune()
        if self.total_cost() + float(cost) > self.daily_limit:
            return True
        return False

    def check_and_add(self, cost: float) -> bool:
        """累加成本并返回是否需降级."""
        should = self.should_fallback(cost)
        self.add_cost(cost)
        return should

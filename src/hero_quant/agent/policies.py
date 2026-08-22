"""Agent 策略：重试、Saga 补偿与预算熔断。

职责：为研究团队图与 Agent 循环提供容错与成本控制原语。
架构位置：agent 层策略组件，被 graph/loop 按需调用，保持轻量无重依赖。
关键设计：
- RetryPolicy：指数退避 + 抖动，可判定 should_retry 并提供 sleep
- error_handler：Saga 补偿入口，映射 NodeError 到 compensate 分支（Command）
- BudgetBreaker：滑动窗口成本熔断，单次或累计超阈即触发降级
"""

from __future__ import annotations

import os
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

    # 默认定价（per 1M tokens）
    DEFAULT_PRICE_IN: float = 0.15
    DEFAULT_PRICE_OUT: float = 0.60

    def _get_prices(self) -> tuple[float, float]:
        """读取定价，环境变量 HERO_LLM_PRICE_IN/OUT 覆盖默认值."""
        try:
            raw_in = os.environ.get("HERO_LLM_PRICE_IN", "").strip()
            price_in = float(raw_in) if raw_in else self.DEFAULT_PRICE_IN
        except Exception:
            price_in = self.DEFAULT_PRICE_IN
        try:
            raw_out = os.environ.get("HERO_LLM_PRICE_OUT", "").strip()
            price_out = float(raw_out) if raw_out else self.DEFAULT_PRICE_OUT
        except Exception:
            price_out = self.DEFAULT_PRICE_OUT
        return price_in, price_out

    def estimate_cost(self, usage: dict) -> float:
        """按 usage 估算成本，兼容多种字段名."""
        if not isinstance(usage, dict):
            return 0.0
        iv = usage.get("input_tokens")
        if iv is None:
            iv = usage.get("prompt_tokens", usage.get("promptTokens", 0))
        ov = usage.get("output_tokens")
        if ov is None:
            ov = usage.get("completion_tokens", usage.get("generated_tokens", 0))
        try:
            iv_f = float(iv) if iv is not None else 0.0
        except Exception:
            iv_f = 0.0
        try:
            ov_f = float(ov) if ov is not None else 0.0
        except Exception:
            ov_f = 0.0
        price_in, price_out = self._get_prices()
        return iv_f * price_in / 1_000_000 + ov_f * price_out / 1_000_000

    def record_usage(self, usage: dict) -> float:
        """记录 usage 并累计成本，返回本次成本."""
        cost = self.estimate_cost(usage)
        # 零成本不追加 _costs，避免无界增长
        if cost is not None:
            try:
                if float(cost) <= 0:
                    return float(cost)
            except Exception:
                # 非数值则按原逻辑处理
                pass
        try:
            self.add_cost(cost)
        except Exception:
            pass
        return cost

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

    def should_fallback(self, cost: float = 0.0) -> bool:
        """滑动窗口熔断：单次或累计超阈即需降级。cost 默认为 0 便于查询累计状态."""
        try:
            c = float(cost) if cost is not None else 0.0
        except Exception:
            c = 0.0
        if c > self.daily_limit:
            return True
        self._prune()
        if self.total_cost() + c > self.daily_limit:
            return True
        return False

    def check_and_add(self, cost: float) -> bool:
        """累加成本并返回是否需降级."""
        should = self.should_fallback(cost)
        self.add_cost(cost)
        return should

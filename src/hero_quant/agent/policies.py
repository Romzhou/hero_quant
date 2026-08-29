"""Agent 策略：重试、Saga 补偿与预算熔断。

职责：为研究团队图与 Agent 循环提供容错与成本控制原语。
架构位置：agent 层策略组件，被 graph/loop 按需调用，保持轻量无重依赖。
关键设计：
- RetryPolicy：指数退避 + 抖动，可判定 should_retry 并提供 sleep
- error_handler：Saga 补偿入口，映射 NodeError 到 compensate 分支（Command）
- BudgetBreaker：滑动窗口成本熔断，单次或累计超阈即触发降级
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Tuple, Type

logger = logging.getLogger(__name__)


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
        # 校验 retry_on 合法性
        try:
            retry_on = self.retry_on
            if not isinstance(retry_on, tuple):
                logger.warning("retry_on not a tuple: %r", retry_on)
                return False
            for t in retry_on:
                if not isinstance(t, type):
                    logger.warning("retry_on contains non-type: %r", t)
                    return False
        except Exception as exc:
            logger.warning("retry_on validation failed: %s", exc, exc_info=True)
            return False
        try:
            return isinstance(exc, retry_on)
        except TypeError as exc:
            logger.warning("isinstance check failed for retry_on %r: %s", retry_on, exc, exc_info=True)
            return False

    def backoff(self, attempt: int) -> float:
        """计算指数退避时长，附加随机抖动."""
        base = self.backoff_base * (self.backoff_factor ** max(0, attempt - 1))
        try:
            j = random.uniform(0, base * self.jitter)
        except Exception as exc:
            logger.warning("jitter calc failed: %s", exc, exc_info=True)
            j = 0
        return base + j

    def sleep(self, attempt: int) -> None:
        d = self.backoff(attempt)
        try:
            time.sleep(d)
        except Exception as exc:
            logger.warning("RetryPolicy.sleep interrupted: %s", exc, exc_info=True)

    async def asleep(self, attempt: int) -> None:
        """异步退避，保留 sync sleep 供同步路径使用，async 路径 await asyncio.sleep."""
        d = self.backoff(attempt)
        try:
            await asyncio.sleep(d)
        except Exception as exc:
            logger.warning("RetryPolicy.asleep interrupted: %s", exc, exc_info=True)


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
    except Exception as exc:
        logger.debug("langgraph.types.Command import failed: %s", exc)
        try:
            from langgraph.graph import Command as _LG2  # type: ignore

            return _LG2
        except Exception as exc2:
            logger.debug("langgraph.graph.Command import failed: %s", exc2)
            return LGCommand


def error_handler(state: dict[str, Any], error: BaseException) -> Any:
    """Saga 补偿入口，返回指向 compensate 的 Command，占位实现确定性回退."""
    goto = "compensate"
    LG = _get_lg_command()
    try:
        return LG(goto=goto, update={"error": str(error)})
    except Exception as exc:
        logger.warning("LG Command construction failed: %s", exc, exc_info=True)
        return {"goto": goto, "update": {"error": str(error)}}


@dataclass
class BudgetBreaker:
    """滑动窗口预算熔断器，按日限额判定是否降级."""

    daily_limit: float = 5.0
    window_seconds: int = 86400
    _costs: list[tuple[float, float]] = field(default_factory=list)  # (ts, cost)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    # 默认定价（per 1M tokens）
    DEFAULT_PRICE_IN: float = 0.15
    DEFAULT_PRICE_OUT: float = 0.60

    def _get_prices(self) -> tuple[float, float]:
        """读取定价，环境变量 HERO_LLM_PRICE_IN/OUT 覆盖默认值."""
        try:
            raw_in = os.environ.get("HERO_LLM_PRICE_IN", "").strip()
            price_in = float(raw_in) if raw_in else self.DEFAULT_PRICE_IN
        except Exception as exc:
            logger.warning("invalid HERO_LLM_PRICE_IN %r: %s", raw_in, exc, exc_info=True)
            price_in = self.DEFAULT_PRICE_IN
        try:
            raw_out = os.environ.get("HERO_LLM_PRICE_OUT", "").strip()
            price_out = float(raw_out) if raw_out else self.DEFAULT_PRICE_OUT
        except Exception as exc:
            logger.warning("invalid HERO_LLM_PRICE_OUT %r: %s", raw_out, exc, exc_info=True)
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
        except Exception as exc:
            logger.warning("invalid input_tokens %r: %s", iv, exc, exc_info=True)
            iv_f = 0.0
        try:
            ov_f = float(ov) if ov is not None else 0.0
        except Exception as exc:
            logger.warning("invalid output_tokens %r: %s", ov, exc, exc_info=True)
            ov_f = 0.0
        price_in, price_out = self._get_prices()
        return iv_f * price_in / 1_000_000 + ov_f * price_out / 1_000_000

    def record_usage(self, usage: dict) -> float:
        """记录 usage 并累计成本，返回本次成本."""
        cost = self.estimate_cost(usage)
        # isfinite 校验 NaN/Inf
        try:
            cf = float(cost) if cost is not None else 0.0
            if not math.isfinite(cf):
                logger.warning("non-finite cost %r, ignoring", cost)
                return 0.0
        except Exception as exc:
            logger.warning("record_usage cost parse failed %r: %s", cost, exc, exc_info=True)
            return 0.0
        # 零成本不追加 _costs，避免无界增长
        if cf <= 0:
            return cf
        try:
            self.add_cost(cf)
        except Exception as exc:
            logger.warning("add_cost failed for %r: %s", cf, exc, exc_info=True)
        return cf

    def _prune_locked(self) -> None:
        """内部 prune，不加锁，调用方需已持有 _lock. 使用 monotonic 避免系统时间回拨影响窗口."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        self._costs = [(ts, c) for ts, c in self._costs if ts >= cutoff]

    def _prune(self) -> None:
        with self._lock:
            self._prune_locked()

    def add_cost(self, cost: float) -> None:
        try:
            c = float(cost) if cost is not None else 0.0
        except Exception as exc:
            logger.warning("add_cost invalid cost %r: %s", cost, exc, exc_info=True)
            return
        if not math.isfinite(c):
            logger.warning("add_cost non-finite cost %r ignored", cost)
            return
        with self._lock:
            self._prune_locked()
            self._costs.append((time.monotonic(), c))

    def total_cost(self) -> float:
        with self._lock:
            self._prune_locked()
            return sum(c for _, c in self._costs)

    def should_fallback(self, cost: float = 0.0) -> bool:
        """滑动窗口熔断：单次或累计超阈即需降级。cost 默认为 0 便于查询累计状态."""
        try:
            c = float(cost) if cost is not None else 0.0
        except Exception as exc:
            logger.warning("should_fallback invalid cost %r: %s", cost, exc, exc_info=True)
            c = 0.0
        if not math.isfinite(c):
            logger.warning("should_fallback non-finite cost %r coerced to 0", cost)
            c = 0.0
        if c > self.daily_limit:
            return True
        with self._lock:
            self._prune_locked()
            total = sum(cc for _, cc in self._costs)
            if total + c > self.daily_limit:
                return True
        return False

    def check_and_add(self, cost: float) -> bool:
        """累加成本并返回是否需降级。原子 check_and_add."""
        try:
            c = float(cost) if cost is not None else 0.0
        except Exception as e:
            raise ValueError(f"invalid cost: {cost}") from e
        if not math.isfinite(c):
            raise ValueError(f"non-finite cost: {cost}")
        with self._lock:
            self._prune_locked()
            total = sum(cc for _, cc in self._costs)
            should = c > self.daily_limit or (total + c > self.daily_limit)
            self._costs.append((time.monotonic(), c))
            return should

"""auth.quota — 租户级配额（请求速率 + 每日 token 预算），Redis 实现。

职责：按 tenant_id 做滑动窗口限流与日预算记账，超限抛 QuotaExceeded（429 + Retry-After）。
架构位置：auth 包；由路由 Depends 或 LLM 调用后记账处使用。
关键设计：
- 无 Redis 时抛 AuthConfigError（503）而不是 429：后端不可用与用户超限必须区分；
- 日预算用 INCRBY 后超额回滚，记账误差仅限并发窗口内的极小重叠，不放大权限；
- 预算按 UTC 自然日切分，键含日期并首次写入设置 TTL，避免无界增长；
- 成本上限先复用 token 口径（BudgetBreaker 的 tenant 维度接入留待 W2 串行窗口）。
"""

from __future__ import annotations

import inspect
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends

from hero_quant.auth.deps import get_current_principal
from hero_quant.auth.errors import AuthConfigError, MissingTenant, QuotaExceeded
from hero_quant.auth.principal import Principal
from hero_quant.infra.redis import RateLimiter, get_redis

try:  # 中文：与 infra/redis.py 同口径，补齐 redis-py 异常基类。
    from redis.exceptions import RedisError as _RedisError

    _REDIS_ERRORS: tuple[type[BaseException], ...] = (
        _RedisError,
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        RuntimeError,
    )
except ImportError:  # pragma: no cover — 未安装 redis-py 时退化为标准异常元组
    _REDIS_ERRORS = (OSError, ValueError, TypeError, AttributeError, RuntimeError)

logger = logging.getLogger(__name__)

DEFAULT_REQUESTS_PER_MINUTE = 60
DEFAULT_DAILY_TOKEN_BUDGET = 2_000_000
_KEY_PREFIX = "hero:quota:"


@dataclass(frozen=True)
class QuotaPolicy:
    """租户配额策略；daily_token_budget<=0 表示不限 token（速率仍生效）。"""

    requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE
    daily_token_budget: int = DEFAULT_DAILY_TOKEN_BUDGET
    window_seconds: int = 60

    def __post_init__(self) -> None:
        if int(self.requests_per_minute) <= 0:
            raise ValueError("requests_per_minute must be > 0")
        if int(self.window_seconds) <= 0:
            raise ValueError("window_seconds must be > 0")


async def _maybe_await(value: Any) -> Any:
    """兼容 sync/async Redis 客户端返回值。"""
    if inspect.isawaitable(value):
        return await value
    return value


class TenantQuotaLimiter:
    """租户速率与日预算门；日预算按 UTC 自然日切分。"""

    def __init__(self, policy: QuotaPolicy | None = None, *, key_prefix: str = _KEY_PREFIX) -> None:
        self._policy = policy or QuotaPolicy()
        self._prefix = key_prefix
        self._rate = RateLimiter(key_prefix=f"{key_prefix}rate:")

    @property
    def policy(self) -> QuotaPolicy:
        return self._policy

    async def check_request(self, tenant_id: str) -> None:
        """滑动窗口限流；超限抛 QuotaExceeded，无后端抛 AuthConfigError。"""
        tenant = self._require_tenant(tenant_id)
        if await get_redis() is None:
            raise AuthConfigError("quota_backend_unavailable")
        allowed = await self._rate.try_acquire(
            f"tenant:{tenant}:request",
            self._policy.requests_per_minute,
            self._policy.window_seconds,
        )
        if not allowed:
            raise QuotaExceeded("tenant_rate_limit_exceeded", retry_after=self._policy.window_seconds)

    async def consume_tokens(self, tenant_id: str, tokens: int) -> int:
        """记入当日 token 用量；超预算回滚并抛 QuotaExceeded，返回当前用量。"""
        tenant = self._require_tenant(tenant_id)
        try:
            amount = int(tokens)
        except (TypeError, ValueError) as e:
            raise ValueError("tokens must be an integer") from e
        if amount <= 0:
            return await self.usage(tenant)
        budget = int(self._policy.daily_token_budget)
        if budget <= 0:
            return await self.usage(tenant)
        client = await get_redis()
        if client is None:
            raise AuthConfigError("quota_backend_unavailable")
        key = self._token_key(tenant)
        try:
            used = int(await _maybe_await(client.incrby(key, amount)))
        except _REDIS_ERRORS as e:
            raise AuthConfigError("quota_backend_error") from e
        if used > budget:
            try:
                await _maybe_await(client.decrby(key, amount))
            except _REDIS_ERRORS as e:  # pragma: no cover — 回滚失败仅影响记账精度
                logger.warning("auth.quota_rollback_failed tenant=%s error=%s", tenant, e)
            raise QuotaExceeded("tenant_daily_token_budget_exceeded", retry_after=_seconds_until_utc_midnight())
        if used == amount:
            try:
                await _maybe_await(client.expire(key, 2 * 24 * 3600))
            except _REDIS_ERRORS as e:  # pragma: no cover — TTL 失败仅影响键回收
                logger.warning("auth.quota_expire_failed tenant=%s error=%s", tenant, e)
        return used

    async def usage(self, tenant_id: str) -> int:
        """当日已用 token 数（读失败按 0 处理，仅用于观测，不参与放行判定）。"""
        client = await get_redis()
        if client is None:
            return 0
        try:
            raw = await _maybe_await(client.get(self._token_key(self._require_tenant(tenant_id))))
        except _REDIS_ERRORS:
            return 0
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0

    def _token_key(self, tenant_id: str) -> str:
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        return f"{self._prefix}daily:{tenant_id}:{day}:tokens"

    @staticmethod
    def _require_tenant(tenant_id: str) -> str:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise MissingTenant("tenant context is not bound")
        return tenant_id.strip()


def _seconds_until_utc_midnight() -> int:
    """距 UTC 零点秒数（Retry-After 用，至少 1）。"""
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((tomorrow - now).total_seconds()))


_default_lock = threading.Lock()
_default_limiter: TenantQuotaLimiter | None = None


def default_quota() -> TenantQuotaLimiter:
    """进程级默认配额器（env 经 config.settings 统一解析；首次调用构建）。"""
    global _default_limiter
    if _default_limiter is not None:
        return _default_limiter
    with _default_lock:
        if _default_limiter is None:
            from hero_quant.config.settings import get_settings

            settings = get_settings()
            _default_limiter = TenantQuotaLimiter(
                QuotaPolicy(
                    requests_per_minute=settings.tenant_requests_per_minute,
                    daily_token_budget=settings.tenant_daily_token_budget,
                )
            )
        return _default_limiter


def reset_default_quota() -> None:
    """清空进程级默认配额器（测试用）。"""
    global _default_limiter
    with _default_lock:
        _default_limiter = None


async def tenant_rate_limit(principal: Principal = Depends(get_current_principal)) -> None:
    """FastAPI 依赖：按当前租户限流（429 由 AuthError 处理器统一映射）。"""
    await default_quota().check_request(principal.tenant_id)


__all__ = [
    "DEFAULT_DAILY_TOKEN_BUDGET",
    "DEFAULT_REQUESTS_PER_MINUTE",
    "QuotaPolicy",
    "TenantQuotaLimiter",
    "default_quota",
    "reset_default_quota",
    "tenant_rate_limit",
]

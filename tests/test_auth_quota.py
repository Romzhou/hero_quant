"""W1/PR-105：租户速率与每日 token 预算（fakeredis，不连真 Redis）。

验证：超限 429 语义（QuotaExceeded + retry_after）、超额回滚、租户隔离、无后端 503。
"""

from __future__ import annotations

import asyncio

import fakeredis.aioredis as fakeredis_async
import pytest

import hero_quant.auth.quota as quota
from hero_quant.auth.errors import AuthConfigError, MissingTenant, QuotaExceeded
from hero_quant.auth.quota import QuotaPolicy, TenantQuotaLimiter, default_quota, reset_default_quota


@pytest.fixture()
def fake_redis():
    import hero_quant.infra.redis as rmod

    rmod.clear_redis_instance()
    fake = fakeredis_async.FakeRedis(decode_responses=True)
    rmod.set_redis_instance(fake, for_async=True)
    try:
        yield fake
    finally:
        rmod.clear_redis_instance()


def test_rate_limit_allows_then_rejects(fake_redis):
    limiter = TenantQuotaLimiter(QuotaPolicy(requests_per_minute=2, daily_token_budget=100))

    async def run():
        await limiter.check_request("tenant-a")
        await limiter.check_request("tenant-a")
        with pytest.raises(QuotaExceeded) as exc:
            await limiter.check_request("tenant-a")
        return exc.value

    error = asyncio.run(run())
    assert error.retry_after == 60


def test_daily_token_budget_and_rollback(fake_redis):
    limiter = TenantQuotaLimiter(QuotaPolicy(requests_per_minute=10, daily_token_budget=100))

    async def run():
        assert await limiter.consume_tokens("tenant-a", 60) == 60
        assert await limiter.consume_tokens("tenant-a", 30) == 90
        with pytest.raises(QuotaExceeded) as exc:
            await limiter.consume_tokens("tenant-a", 20)
        return await limiter.usage("tenant-a"), exc.value

    used, error = asyncio.run(run())
    assert used == 90
    assert error.retry_after >= 1


def test_tenants_are_isolated(fake_redis):
    limiter = TenantQuotaLimiter(QuotaPolicy(requests_per_minute=10, daily_token_budget=100))

    async def run():
        await limiter.consume_tokens("tenant-a", 80)
        await limiter.consume_tokens("tenant-b", 10)
        return await limiter.usage("tenant-a"), await limiter.usage("tenant-b")

    assert asyncio.run(run()) == (80, 10)


def test_missing_tenant_rejected():
    limiter = TenantQuotaLimiter()
    with pytest.raises(MissingTenant):
        asyncio.run(limiter.check_request("  "))


def test_no_redis_backend_is_config_error(monkeypatch):
    async def _no_backend():
        return None

    monkeypatch.setattr(quota, "get_redis", _no_backend)
    limiter = TenantQuotaLimiter()
    with pytest.raises(AuthConfigError):
        asyncio.run(limiter.check_request("tenant-a"))


def test_invalid_policy_rejected():
    with pytest.raises(ValueError):
        QuotaPolicy(requests_per_minute=0)
    with pytest.raises(ValueError):
        QuotaPolicy(window_seconds=0)


def test_default_quota_reads_env(monkeypatch):
    monkeypatch.setenv("HERO_TENANT_REQUESTS_PER_MINUTE", "7")
    monkeypatch.setenv("HERO_TENANT_DAILY_TOKEN_BUDGET", "12345")
    from hero_quant.config.settings import get_settings

    get_settings.cache_clear()
    reset_default_quota()
    try:
        limiter = default_quota()
        assert limiter.policy.requests_per_minute == 7
        assert limiter.policy.daily_token_budget == 12345
    finally:
        reset_default_quota()

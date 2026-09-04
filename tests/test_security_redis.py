"""Phase 0: Redis-backed ticket tests — uses fakeredis, no real Redis required."""

import time

import fakeredis
import pytest


def _reset():
    import hero_quant.infra.redis as rmod

    rmod.clear_redis_instance()
    fake = fakeredis.FakeRedis(decode_responses=True)
    rmod.set_redis_instance(fake)
    return fake


def test_ticket_single_consume():
    fake = _reset()
    from hero_quant.api.security import issue_ticket, consume_ticket

    t = issue_ticket(ttl=60)
    assert consume_ticket(t) is True
    assert consume_ticket(t) is False  # replay
    fake.flushall()


def test_ticket_distributed_via_redis():
    fake = _reset()
    from hero_quant.api.security import issue_ticket, consume_ticket

    t = issue_ticket(ttl=60)
    # Key exists in redis
    assert fake.exists(f"hero:ticket:{t}") == 1
    assert consume_ticket(t) is True
    assert fake.exists(f"hero:ticket:{t}") == 0
    fake.flushall()


def test_ticket_expire():
    fake = _reset()
    from hero_quant.api.security import issue_ticket, consume_ticket

    t = issue_ticket(ttl=1)
    time.sleep(1.2)
    # fakeredis should expire
    assert consume_ticket(t) is False
    fake.flushall()


def test_ticket_concurrent_consume_only_one_wins():
    fake = _reset()
    from hero_quant.api.security import issue_ticket, consume_ticket

    t = issue_ticket(ttl=60)
    results = [consume_ticket(t) for _ in range(5)]
    assert sum(results) == 1
    fake.flushall()


def test_rate_limiter_blocks_after_limit():
    fake = _reset()
    from hero_quant.infra.redis import RateLimiter

    limiter = RateLimiter()
    key = "test:ip:127.0.0.1"
    for _ in range(10):
        assert limiter.try_acquire_sync(key, 10, 60) is True
    assert limiter.try_acquire_sync(key, 10, 60) is False
    fake.flushall()


def test_cache_decorator():
    fake = _reset()
    import asyncio

    from hero_quant.infra.redis import cache

    calls = {"n": 0}

    @cache("test:cache", expire=60)
    async def fetch(x: int):
        calls["n"] += 1
        return {"v": x * 2}

    async def _run():
        r1 = await fetch(21)
        r2 = await fetch(21)
        assert r1 == {"v": 42}
        assert r2 == {"v": 42}
        assert calls["n"] == 1  # second hit from cache

    asyncio.run(_run())
    fake.flushall()

"""PR3-G TDD: slowapi三档限流 + Session + Counter + Stream消费者组 (fakeredis, 无需真实 Redis)."""

import asyncio
from types import SimpleNamespace

import fakeredis.aioredis as fakeredis_async
import pytest
from fastapi import HTTPException


@pytest.fixture()
def _redis():
    import hero_quant.infra.redis as rmod

    rmod.clear_redis_instance()
    fake = fakeredis_async.FakeRedis(decode_responses=True)
    rmod.set_redis_instance(fake)
    yield fake
    try:
        asyncio.run(fake.flushall())
    except Exception:
        pass
    rmod.clear_redis_instance()


def _req(user_id=None, ip="127.0.0.1"):
    user = SimpleNamespace(id=user_id) if user_id is not None else None
    return SimpleNamespace(state=SimpleNamespace(current_user=user), client=SimpleNamespace(host=ip))


def test_limit_key_prefers_user_id(_redis):
    from hero_quant.api.rate_limiter import limit_key

    assert limit_key(_req(user_id="u42")) == "user:u42"
    assert limit_key(_req(ip="10.0.0.9")) == "ip:10.0.0.9"


@pytest.mark.parametrize(
    "quota_key,dep_name",
    [("CHAT_MAX", "rate_limit_chat"), ("SESSION_MAX", "rate_limit_session"), ("TOOL_MAX", "rate_limit_tool")],
)
def test_tier_quota_max_plus_one_false(_redis, quota_key, dep_name):
    import hero_quant.api.rate_limiter as rl
    from hero_quant.infra.redis import RateLimiter

    quota = getattr(rl, quota_key)
    dep = getattr(rl, dep_name)

    async def _run():
        limiter = RateLimiter()
        key = f"pr3g:tier:{dep_name}"
        for _ in range(quota):
            assert await limiter.try_acquire(key, quota, 60) is True
        assert await limiter.try_acquire(key, quota, 60) is False  # max+1 次拒绝
        # Depends 胶水走同一配额：打满后抛 429
        req = _req(user_id=f"pr3g-{dep_name}")
        for _ in range(quota):
            assert await dep(req) is True
        with pytest.raises(HTTPException) as exc:
            await dep(req)
        assert exc.value.status_code == 429

    asyncio.run(_run())


def test_tier_quotas_match_spec(_redis):
    import hero_quant.api.rate_limiter as rl

    assert (rl.CHAT_MAX, rl.SESSION_MAX, rl.TOOL_MAX) == (10, 30, 60)


def test_session_set_get_ttl_7d(_redis):
    from hero_quant.infra.session import SESSION_TTL, get_session, set_session

    assert SESSION_TTL == 7 * 24 * 3600

    async def _run():
        assert await get_session("pr3g-missing") is None
        assert await set_session("s1", {"user": "u1", "cart": [1, 2]}) is True
        assert await get_session("s1") == {"user": "u1", "cart": [1, 2]}
        ttl = await _redis.ttl("hero:session:s1")
        assert 0 < ttl <= SESSION_TTL

    asyncio.run(_run())


def test_counter_incr_atomic(_redis):
    from hero_quant.infra.redis import Counter

    async def _run():
        c = Counter()
        assert await c.incr("pr3g:hits") == 1
        assert await c.incr("pr3g:hits") == 2
        assert await c.incr("pr3g:hits", 3) == 5

    asyncio.run(_run())


def test_stream_consume_then_ack(_redis):
    from hero_quant.infra.redis import RedisStream

    async def _run():
        stream = RedisStream()
        await stream.publish("hero:stream:pr3g", {"kind": "trace", "data": "{}"})
        entries = await stream.subscribe_consumer(
            "hero:stream:pr3g", "pr3g-group", "pr3g-consumer", count=10, block=200
        )
        assert entries, "consumer got no entries"
        ids = [mid for mid, _ in entries]
        assert await stream.ack("hero:stream:pr3g", "pr3g-group", *ids) >= 1

    asyncio.run(_run())

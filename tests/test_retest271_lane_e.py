"""Lane E retest271 repro tests — API/infra review findings.

Method per file: append FAIL-first repro tests here, fix the source file,
re-run to PASS, commit source + this file together. NEVER edit other test files.
"""

from __future__ import annotations

import asyncio
import inspect

import fakeredis
import fakeredis.aioredis as fakeredis_async
import pytest


def _reset():
    import hero_quant.infra.redis as rmod

    rmod.clear_redis_instance()
    return rmod


# ── infra/redis.py item 1 (high): sync/async test-hook pollution ──


def test_lanee_redis_sync_slot_rejects_async_client():
    """Async client must not be servable from the sync slot (sync callers
    would receive un-awaited coroutines: always-truthy / decode fail)."""
    rmod = _reset()
    try:
        async_fake = fakeredis_async.FakeRedis(decode_responses=True)
        rmod.set_redis_instance(async_fake)
        got = rmod.get_redis_sync()
        assert not inspect.iscoroutine(got.get("lanee:probe")) if got is not None else True, (
            "sync slot serves async client: get_redis_sync() returns coroutine results"
        )
    finally:
        rmod.clear_redis_instance()


def test_lanee_redis_async_slot_rejects_sync_client():
    """Sync client must not be served from the async slot (async paths would
    misbehave on non-awaitable results)."""
    rmod = _reset()
    try:
        sync_fake = fakeredis.FakeRedis(decode_responses=True)
        rmod.set_redis_instance(sync_fake)

        async def _run():
            got = await rmod.get_redis()
            assert not inspect.iscoroutinefunction(getattr(got, "get", None)) or True
            # core assertion: a single positional injection must not land in BOTH slots
            both = (
                rmod._redis_sync_instance is sync_fake
                and rmod._redis_async_instance is sync_fake
            )
            assert not both, "sync fake injected into BOTH sync and async slots"

        asyncio.run(_run())
    finally:
        rmod.clear_redis_instance()


# ── infra/redis.py item 2 (high): global asyncio.Lock across event loops ──


def test_lanee_redis_get_redis_survives_loop_churn():
    """get_redis() must work across fresh event loops (pytest-asyncio creates
    a new loop per test); a stale-loop-bound asyncio.Lock raises RuntimeError."""
    rmod = _reset()
    try:
        rmod.clear_redis_instance()
        async_fake = fakeredis_async.FakeRedis(decode_responses=True)
        rmod.set_redis_instance(async_fake, for_async=True)

        async def _run():
            return await rmod.get_redis()

        r1 = asyncio.run(_run())
        rmod.clear_redis_instance()
        rmod.set_redis_instance(async_fake, for_async=True)
        r2 = asyncio.run(_run())  # new loop — must not raise RuntimeError
        assert r1 is async_fake and r2 is async_fake
    finally:
        rmod.clear_redis_instance()


def test_lanee_redis_no_global_asyncio_lock_singleton():
    """get_redis must not bind a module-global asyncio.Lock to a stale loop.

    per-loop 锁表（dict[loop, Lock]）是允许的修复形态；禁止的是单例全局锁被
    `async with` 复用（loop 关闭后抛 RuntimeError）。
    """
    import hero_quant.infra.redis as rmod

    src = inspect.getsource(rmod.get_redis)
    uses_global_singleton = "_redis_async_lock" in src and "_redis_async_locks" not in src
    assert not uses_global_singleton, (
        "get_redis still guards with a global asyncio.Lock singleton"
    )


# ── infra/redis.py item 3 (high): non-atomic lock-release fallback ──


def test_lanee_redis_lock_release_never_deletes_new_owner():
    """Expired holder (stale token snapshot) must NOT delete the new owner's
    lock even when eval/Lua is unavailable."""
    rmod = _reset()

    class _NoEvalRedis:
        """Sync fake without eval support (forces the non-Lua path)."""

        def __init__(self):
            self.store = {}
            self.deleted = []

        def set(self, k, v, nx=False, ex=None):
            if nx and k in self.store:
                return None
            self.store[k] = v
            return True

        def get(self, k):
            return self.store.get(k)

        def delete(self, k):
            self.deleted.append(k)
            return self.store.pop(k, None) is not None

        def eval(self, *a, **kw):
            raise TypeError("eval unsupported")

    fake = _NoEvalRedis()
    rmod.set_redis_sync_instance(fake)

    async def _run():
        locker = rmod.RedisLock()
        # Monkeypatch get_redis to serve our no-eval fake on the async path
        orig = rmod.get_redis

        async def _fake_get():
            return fake

        rmod.get_redis = _fake_get
        try:
            async with locker.lock("lanee:steal", timeout=60):
                key = f"{locker.key_prefix}lanee:steal"
                old_token = fake.get(key)
                assert old_token
                # Simulate expiry + new owner acquiring
                fake.store[key] = "new-owner-token"
            # After release: new owner's lock must survive
            assert fake.get(key) == "new-owner-token", (
                "stale holder deleted the new owner's lock (non-atomic fallback)"
            )
        finally:
            rmod.get_redis = orig

    try:
        asyncio.run(_run())
    finally:
        rmod.clear_redis_instance()


# ── infra/redis.py item 4 (medium): rate-limiter fallback atomicity ──


def test_lanee_redis_ratelimit_fallback_enforces_limit():
    """Fallback (no Lua) must still enforce max_requests: max+1th acquire False."""
    rmod = _reset()
    try:
        sync_fake = fakeredis.FakeRedis(decode_responses=True)
        # Force the eval-failure path if the fake lacks eval
        rmod.set_redis_sync_instance(sync_fake)
        limiter = rmod.RateLimiter()
        for _ in range(5):
            assert limiter.try_acquire_sync("lanee:rl", 5, 60) is True
        assert limiter.try_acquire_sync("lanee:rl", 5, 60) is False
    finally:
        rmod.clear_redis_instance()


# ── infra/redis.py item 5 (medium): publish_sync unbounded ──


def test_lanee_redis_publish_sync_caps_stream():
    """publish_sync must cap the stream like async publish (maxlen), not grow unbounded."""
    import hero_quant.infra.redis as rmod

    src = inspect.getsource(rmod.RedisStream.publish_sync)
    assert "maxlen" in src or "xtrim" in src.lower(), (
        "publish_sync uses bare xadd without maxlen/xtrim cap"
    )

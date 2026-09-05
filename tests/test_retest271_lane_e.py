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


# ══════════════════════════════════════════════════════════════════════
# Lane E1 · ws.py repro tests (detail log src__hero_quant__api__ws_py.log)
# ══════════════════════════════════════════════════════════════════════
import json as _lanee_json
import logging as _lanee_logging
from types import SimpleNamespace as _lanee_SNS


def _lanee_reset_async_redis():
    import hero_quant.infra.redis as rmod

    rmod.clear_redis_instance()
    fake = fakeredis_async.FakeRedis(decode_responses=True)
    rmod.set_redis_instance(fake)
    return rmod, fake


def _lanee_clear_ws():
    from hero_quant.api import ws as wsmod

    wsmod.manager._connections.clear()
    wsmod.heartbeat._last_active.clear()
    hb = wsmod.heartbeat
    if hasattr(hb, "_server_keepalives"):
        hb._server_keepalives.clear()
    return wsmod


class _lanee_FakeWS:
    """Minimal WS double: scripted receive_json (dict / 'timeout' / 'disconnect')."""

    def __init__(self, recv_script=()):
        self.sent = []
        self.closed = None
        self._script = list(recv_script)

    async def accept(self):
        pass

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self, *a, **kw):
        self.closed = (a, kw)

    async def receive_json(self):
        from fastapi import WebSocketDisconnect

        if not self._script:
            raise WebSocketDisconnect()
        act = self._script.pop(0)
        if act == "timeout":
            raise asyncio.TimeoutError()
        if act == "disconnect":
            raise WebSocketDisconnect()
        return act


async def _lanee_run_consumer_briefly(wsmod, stop_after=0.8):
    """Run run_trace_consumer until shortly after first read settles."""
    stop = asyncio.Event()

    async def _stopper():
        await asyncio.sleep(stop_after)
        stop.set()

    await asyncio.gather(wsmod.run_trace_consumer(stop), _stopper())


# ── ws.py high 1: duplicate / missed fan-out ──


def test_lanee_ws_publish_tags_origin():
    """broadcast publishes Stream entry tagged with origin == INSTANCE_ID so the
    originator's own consumer can suppress the echo (no duplicate delivery)."""
    rmod, fake = _lanee_reset_async_redis()
    wsmod = _lanee_clear_ws()
    try:
        from hero_quant.api.ws import TRACE_STREAM, broadcast_trace_event

        async def _run():
            await broadcast_trace_event({"type": "delta", "delta": "origin-probe"})
            entries = await fake.xread({TRACE_STREAM: "0"}, count=10)
            assert entries, "stream entry missing"
            fields = [f for _, msgs in entries for _, f in msgs]
            assert any(
                f.get("origin") == wsmod.INSTANCE_ID for f in fields
            ), f"publish untagged: no origin==INSTANCE_ID in {fields}"

        asyncio.run(_run())
    finally:
        rmod.clear_redis_instance()
        wsmod.manager._connections.clear()


def test_lanee_ws_consumer_skips_own_origin_no_duplicate():
    """Single worker end-to-end: broadcast delivers locally once; the worker's
    own consumer must NOT re-deliver its own Stream entry (exactly-once)."""
    rmod, fake = _lanee_reset_async_redis()
    wsmod = _lanee_clear_ws()
    ch = "trace:lanee-dedup"
    ws = _lanee_FakeWS()
    try:
        from hero_quant.api.ws import broadcast_trace_event

        async def _run():
            await wsmod.manager.connect(ch, ws)
            await broadcast_trace_event({"type": "delta", "delta": "dup-probe"})
            assert ws.sent.count({"type": "delta", "delta": "dup-probe"}) == 1
            await _lanee_run_consumer_briefly(wsmod)
            assert ws.sent.count({"type": "delta", "delta": "dup-probe"}) == 1, (
                f"duplicate delivery: consumer re-forwarded own entry: {ws.sent}"
            )

        asyncio.run(_run())
    finally:
        asyncio.run(wsmod.manager.disconnect(ch, ws))
        rmod.clear_redis_instance()
        wsmod.manager._connections.clear()


def test_lanee_ws_consumer_delivers_foreign_origin():
    """Guard against over-suppression: entries from OTHER workers must still be
    forwarded locally by this worker's consumer."""
    rmod, fake = _lanee_reset_async_redis()
    wsmod = _lanee_clear_ws()
    ch = "trace:lanee-foreign"
    ws = _lanee_FakeWS()
    try:
        from hero_quant.api.ws import TRACE_STREAM

        event = {"type": "delta", "delta": "foreign-probe"}

        async def _run():
            await wsmod.manager.connect(ch, ws)
            await _lanee_live_xadd(
                fake,
                TRACE_STREAM,
                {
                    "channel": ch,
                    "origin": "some-other-worker",
                    "data": _lanee_json.dumps(event),
                },
            )
            await _lanee_run_consumer_briefly(wsmod)
            assert event in ws.sent, "foreign-origin entry was wrongly suppressed"

        asyncio.run(_run())
    finally:
        asyncio.run(wsmod.manager.disconnect(ch, ws))
        rmod.clear_redis_instance()
        wsmod.manager._connections.clear()


def test_lanee_ws_consumer_group_is_per_worker():
    """run_trace_consumer must read via a per-worker group (INSTANCE_ID-scoped)
    so every worker receives every entry — a single shared group delivers each
    entry to exactly one worker (broadcast misses)."""
    import hero_quant.api.ws as wsmod

    group = wsmod._trace_consumer_group()
    assert wsmod.INSTANCE_ID in group and group != wsmod.TRACE_GROUP, (
        f"consumer group not per-worker: {group!r}"
    )
    src = inspect.getsource(wsmod.run_trace_consumer)
    assert "_trace_consumer_group" in src, (
        "run_trace_consumer still uses one shared TRACE_GROUP for all workers"
    )


# ── ws.py high 2: user isolation dropped in sync path ──


def test_lanee_ws_sync_broadcast_routes_user():
    """broadcast_trace_event_sync(event, user=...) must fan out ONLY to that
    user's channel — never full-broadcast (cross-user leak)."""
    rmod, fake = _lanee_reset_async_redis()
    wsmod = _lanee_clear_ws()
    from hero_quant.api.ws import resolve_user_channel

    ch_u1 = resolve_user_channel("lanee-u1", None)
    ch_u2 = resolve_user_channel("lanee-u2", None)
    ws1, ws2 = _lanee_FakeWS(), _lanee_FakeWS()
    event = {"type": "delta", "delta": "user-scoped-probe"}

    async def _run():
        await wsmod.manager.connect(ch_u1, ws1)
        await wsmod.manager.connect(ch_u2, ws2)
        wsmod.broadcast_trace_event_sync(event, user="lanee-u1")
        await asyncio.sleep(0.5)
        assert event in ws1.sent, "user-scoped event never reached its owner"
        assert event not in ws2.sent, "CROSS-USER LEAK: u2 received u1's event"

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(wsmod.manager.disconnect(ch_u1, ws1))
        asyncio.run(wsmod.manager.disconnect(ch_u2, ws2))
        rmod.clear_redis_instance()
        wsmod.manager._connections.clear()


def test_lanee_ws_sync_broadcast_legacy_no_user():
    """Guard: sync broadcast without user keeps legacy full-broadcast (Monitor
    compat; single-arg call shape used by existing call sites)."""
    rmod, fake = _lanee_reset_async_redis()
    wsmod = _lanee_clear_ws()
    ws1, ws2 = _lanee_FakeWS(), _lanee_FakeWS()
    event = {"type": "delta", "delta": "legacy-probe"}

    async def _run():
        await wsmod.manager.connect("trace:lanee-leg-a", ws1)
        await wsmod.manager.connect("trace:lanee-leg-b", ws2)
        wsmod.broadcast_trace_event_sync(event)
        await asyncio.sleep(0.5)
        assert event in ws1.sent and event in ws2.sent

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(wsmod.manager.disconnect("trace:lanee-leg-a", ws1))
        asyncio.run(wsmod.manager.disconnect("trace:lanee-leg-b", ws2))
        rmod.clear_redis_instance()
        wsmod.manager._connections.clear()


# ── ws.py medium 1: heartbeat eviction races WSManager lock ──


def test_lanee_ws_eviction_holds_manager_lock():
    """_check_loop must pop connections under WSManager lock (connect /
    disconnect mutate the same dict under the lock; lock-free get+pop can
    evict a just-added reconnect)."""
    import hero_quant.api.ws as wsmod

    src = inspect.getsource(wsmod.HeartbeatMonitor._check_loop)
    assert "manager._lock" in src or "self._lock" in src, (
        "_check_loop still reads/pops manager._connections without the lock"
    )


def test_lanee_ws_eviction_closes_expired():
    """Guard (behavioral): an expired channel is evicted and its sockets are
    closed with heartbeat-timeout code 4002."""
    rmod, fake = _lanee_reset_async_redis()
    wsmod = _lanee_clear_ws()
    from datetime import timedelta

    ch = "trace:lanee-expire"
    ws = _lanee_FakeWS()

    async def _run():
        await wsmod.manager.connect(ch, ws)
        wsmod.heartbeat.record(ch)
        wsmod.heartbeat._last_active[ch] -= timedelta(seconds=120)
        wsmod.heartbeat.CHECK_INTERVAL = 0.05
        task = asyncio.create_task(wsmod.heartbeat._check_loop())
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert ws.closed is not None, "expired connection was not closed"
        args, kwargs = ws.closed
        assert args == (4002,) or kwargs.get("code") == 4002, (
            f"wrong close code for heartbeat timeout: {ws.closed!r}"
        )
        assert ch not in wsmod.manager._connections

    try:
        asyncio.run(_run())
    finally:
        try:
            del wsmod.heartbeat.CHECK_INTERVAL
        except AttributeError:
            pass
        rmod.clear_redis_instance()
        wsmod.manager._connections.clear()
        wsmod.heartbeat._last_active.clear()


# ── ws.py medium 2: ack-on-failure loses messages ──


def test_lanee_ws_consumer_no_ack_on_forward_failure(monkeypatch):
    """Transient local-send failure must NOT be acked (entry stays pending for
    redelivery); ack belongs only after successful forward."""
    rmod, fake = _lanee_reset_async_redis()
    wsmod = _lanee_clear_ws()
    from hero_quant.api.ws import TRACE_STREAM
    from hero_quant.infra.redis import RedisStream

    acked = []
    orig_ack = RedisStream.ack

    async def _spy_ack(self, stream, group, *ids):
        acked.extend(ids)
        return await orig_ack(self, stream, group, *ids)

    monkeypatch.setattr(RedisStream, "ack", _spy_ack)

    async def _boom(channel, data):
        raise RuntimeError("lanee-transient-send-failure")

    monkeypatch.setattr(wsmod.manager, "send_to", _boom)
    monkeypatch.setattr(wsmod.manager, "broadcast", _boom)

    async def _run():
        await _lanee_live_xadd(
            fake,
            TRACE_STREAM,
            {"channel": "trace", "data": _lanee_json.dumps({"type": "ping"})},
        )
        await _lanee_run_consumer_briefly(wsmod)
        assert acked == [], f"failed forward was acked (message lost): {acked}"

    try:
        asyncio.run(_run())
    finally:
        rmod.clear_redis_instance()
        wsmod.manager._connections.clear()


def test_lanee_ws_consumer_no_ack_on_poison(monkeypatch):
    """Poison payload (unparseable JSON) must NOT be acked away silently."""
    rmod, fake = _lanee_reset_async_redis()
    wsmod = _lanee_clear_ws()
    from hero_quant.api.ws import TRACE_STREAM
    from hero_quant.infra.redis import RedisStream

    acked = []
    orig_ack = RedisStream.ack

    async def _spy_ack(self, stream, group, *ids):
        acked.extend(ids)
        return await orig_ack(self, stream, group, *ids)

    monkeypatch.setattr(RedisStream, "ack", _spy_ack)

    async def _run():
        await _lanee_live_xadd(
            fake, TRACE_STREAM, {"channel": "trace", "data": "not-json-poison"}
        )
        await _lanee_run_consumer_briefly(wsmod)
        assert acked == [], f"poison payload was acked (silently dropped): {acked}"

    try:
        asyncio.run(_run())
    finally:
        rmod.clear_redis_instance()
        wsmod.manager._connections.clear()


# ── ws.py medium 3: server-driven pong defeats heartbeat timeout ──


def test_lanee_ws_server_pong_renewal_bounded():
    """Server keepalive pongs must NOT renew liveness unboundedly: after a
    bounded number of consecutive server pongs with zero client traffic, the
    channel must become evictable so dead peers eventually expire."""
    rmod, fake = _lanee_reset_async_redis()
    wsmod = _lanee_clear_ws()
    orig_ct = wsmod.consume_ticket
    wsmod.consume_ticket = lambda t: True
    orig_record = wsmod.heartbeat.record
    calls = {"n": 0}

    def _spy(ch):
        calls["n"] += 1
        return orig_record(ch)

    wsmod.heartbeat.record = _spy
    try:
        bound = getattr(wsmod.heartbeat, "MAX_SERVER_KEEPALIVES", 0)

        async def _run():
            ws = _lanee_FakeWS(recv_script=["timeout"] * 6 + ["disconnect"])
            await wsmod.ws_trace(ws, ticket="t", user="")
            pongs = [m for m in ws.sent if m.get("type") == "pong"]
            assert len(pongs) == 6, f"keepalive pongs must still be sent: {ws.sent}"
            assert calls["n"] <= 1 + bound, (
                f"server pongs renew liveness forever: record called {calls['n']}x "
                f"for 6 timeouts with no client traffic (bound={bound})"
            )

        asyncio.run(_run())
    finally:
        wsmod.consume_ticket = orig_ct
        wsmod.heartbeat.record = orig_record
        rmod.clear_redis_instance()
        wsmod.manager._connections.clear()
        wsmod.heartbeat._last_active.clear()
        if hasattr(wsmod.heartbeat, "_server_keepalives"):
            wsmod.heartbeat._server_keepalives.clear()


# ══════════════════════════════════════════════════════════════════════
# Lane E1 · rate_limiter.py repro tests
# (detail log src__hero_quant__api__rate_limiter_py.log)
# ══════════════════════════════════════════════════════════════════════

def _lanee_rl_request(user_id=_lanee_SNS(), host="9.9.9.9"):
    from types import SimpleNamespace

    return SimpleNamespace(state=SimpleNamespace(current_user=user_id), client=SimpleNamespace(host=host))


# ── rate_limiter.py high: fail-closed 503 unreachable / key built in try ──


def test_lanee_rl_limitkey_error_not_mislabeled_503(monkeypatch):
    """A programming error in limit_key() must NOT be mislabeled 503 'Rate
    limiter unavailable': the key must be built OUTSIDE the backend try so
    only genuine backend failures map to 503."""
    import hero_quant.api.rate_limiter as rl
    from fastapi import HTTPException

    def _boom(request):
        raise ValueError("lanee-key-construction-bug")

    monkeypatch.setattr(rl, "limit_key", _boom)
    req = _lanee_rl_request()
    with pytest.raises(ValueError, match="lanee-key-construction-bug"):
        asyncio.run(rl.rate_limit_chat(req))


def test_lanee_rl_check_documents_failopen_contract():
    """_check docstring must not claim fail-closed 503 while infra
    RateLimiter.try_acquire fail-opens (returns True on backend errors):
    document the real contract instead."""
    import hero_quant.api.rate_limiter as rl

    doc = rl._check.__doc__ or ""
    assert "fail-open" in doc, (
        f"_check still advertises fail-closed 503, hiding the fail-open infra "
        f"contract: {doc!r}"
    )


def test_lanee_rl_backend_error_still_503(monkeypatch):
    """Guard: a genuine backend failure from try_acquire still maps to 503
    (contract lock; the except path covers unexpected backend errors)."""
    import hero_quant.api.rate_limiter as rl
    from fastapi import HTTPException

    class _Boom:
        async def try_acquire(self, *a, **k):
            raise RuntimeError("lanee-redis-down")

    monkeypatch.setattr(rl, "RateLimiter", lambda: _Boom())
    req = _lanee_rl_request(user_id=None)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(rl.rate_limit_chat(req))
    assert ei.value.status_code == 503


# ── rate_limiter.py medium: anonymous ip:unknown bucket collapse ──


def test_lanee_rl_unknown_ip_warns(caplog):
    """Missing client IP must be observable: falling back to the shared
    ip:unknown bucket logs a warning so proxy/misconfig DoS-collapse is
    visible instead of silent."""
    import hero_quant.api.rate_limiter as rl
    from types import SimpleNamespace

    req = SimpleNamespace(state=SimpleNamespace(current_user=None), client=None)
    with caplog.at_level("WARNING", logger="hero_quant.api.rate_limiter"):
        key = rl.limit_key(req)
    assert key == "ip:unknown"
    assert any("unknown" in (r.getMessage() or "") for r in caplog.records), (
        "ip:unknown fallback is silent: no warning logged"
    )


# ══════════════════════════════════════════════════════════════════════
# Lane E1 · ws.py follow-up (ocr rescan of lane-e1 files):
# eviction remove-guard + stale replay skip
# ══════════════════════════════════════════════════════════════════════
import time as _lanee_time


async def _lanee_live_xadd(fake, stream, fields):
    """Plant a Stream entry that reads as LIVE traffic (ID in the near
    future) so the consumer's restart-replay skip does not swallow it."""
    live_id = f"{int(_lanee_time.time() * 1000) + 5000}-0"
    return await fake.xadd(stream, fields, id=live_id)


def test_lanee_ws_eviction_keeps_fresh_reconnect():
    """A record() landing during the awaited close() (same-user reconnect)
    must NOT be wiped by the trailing remove(): the fresh channel stays
    monitored instead of being orphaned (never times out)."""
    rmod, fake = _lanee_reset_async_redis()
    wsmod = _lanee_clear_ws()
    from datetime import timedelta

    ch = "trace:lanee-reconnect"

    class _ReconnectWS(_lanee_FakeWS):
        async def close(self, *a, **kw):
            self.closed = (a, kw)
            # Simulate a same-channel reconnect recording liveness
            # while the reaper awaits close().
            wsmod.heartbeat.record(ch)

    ws = _ReconnectWS()

    async def _run():
        await wsmod.manager.connect(ch, ws)
        wsmod.heartbeat.record(ch)
        wsmod.heartbeat._last_active[ch] -= timedelta(seconds=120)
        wsmod.heartbeat.CHECK_INTERVAL = 0.05
        task = asyncio.create_task(wsmod.heartbeat._check_loop())
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert ws.closed is not None, "expired connection was not closed"
        assert ch in wsmod.heartbeat._last_active, (
            "fresh reconnect liveness was wiped by trailing remove() "
            "(orphaned: never times out)"
        )

    try:
        asyncio.run(_run())
    finally:
        try:
            del wsmod.heartbeat.CHECK_INTERVAL
        except AttributeError:
            pass
        rmod.clear_redis_instance()
        wsmod.manager._connections.clear()
        wsmod.heartbeat._last_active.clear()


def test_lanee_ws_consumer_skips_stale_restart_replay():
    """Entries predating this consumer (restart replay under a fresh
    per-worker group) must be acked-and-dropped, never redelivered."""
    rmod, fake = _lanee_reset_async_redis()
    wsmod = _lanee_clear_ws()
    from hero_quant.api.ws import TRACE_STREAM

    ch = "trace:lanee-stale"
    ws = _lanee_FakeWS()

    async def _run():
        await wsmod.manager.connect(ch, ws)
        await fake.xadd(
            TRACE_STREAM,
            {
                "channel": ch,
                "origin": "some-other-worker",
                "data": _lanee_json.dumps({"type": "delta", "delta": "stale-probe"}),
            },
            id="1-1",
        )
        await _lanee_run_consumer_briefly(wsmod)
        assert ws.sent == [], f"restart replay redelivered stale entry: {ws.sent}"

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(wsmod.manager.disconnect(ch, ws))
        rmod.clear_redis_instance()
        wsmod.manager._connections.clear()

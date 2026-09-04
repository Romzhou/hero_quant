"""PR1-B TDD: ws 多实例广播 — fakeredis 验证 Stream 发布/在线心跳/单机广播不变."""

import asyncio
import json

import fakeredis.aioredis as fakeredis_async


def _reset():
    import hero_quant.infra.redis as rmod

    rmod.clear_redis_instance()
    fake = fakeredis_async.FakeRedis(decode_responses=True)
    rmod.set_redis_instance(fake)
    return fake


def _clear_manager():
    from hero_quant.api.ws import manager

    manager._connections.clear()
    return manager


class _FakeWS:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def accept(self):
        pass

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self, *a, **kw):
        self.closed = True


def test_broadcast_publishes_trace_stream():
    fake = _reset()
    _clear_manager()
    from hero_quant.api.ws import TRACE_STREAM, broadcast_trace_event

    event = {"type": "tool", "tool": "demo", "status": "running"}

    async def _run():
        await broadcast_trace_event(event)
        length = await fake.xlen(TRACE_STREAM)
        assert length >= 1
        entries = await fake.xread({TRACE_STREAM: "0"}, count=10)
        assert entries, "stream entries missing"
        payloads = [fields for _, msgs in entries for _, fields in msgs]
        assert any("channel" in p and "data" in p for p in payloads)
        matched = [p for p in payloads if "data" in p]
        assert any(json.loads(p["data"]) == event for p in matched)
        await fake.flushall()

    asyncio.run(_run())


def test_online_heartbeat_set_ex():
    fake = _reset()
    from hero_quant.api.ws import ONLINE_PREFIX, ONLINE_TTL, mark_online

    channel = "trace:probe"

    async def _run():
        await mark_online(channel)
        key = f"{ONLINE_PREFIX}{channel}"
        assert await fake.exists(key) == 1
        ttl = await fake.ttl(key)
        assert 0 < ttl <= ONLINE_TTL <= 90
        await fake.flushall()

    asyncio.run(_run())


def test_local_broadcast_still_delivers():
    _reset()
    manager = _clear_manager()
    from hero_quant.api.ws import broadcast_trace_event

    ws1, ws2 = _FakeWS(), _FakeWS()
    event = {"type": "delta", "delta": "hello"}

    async def _run():
        await manager.connect("trace:a", ws1)
        await manager.connect("trace:b", ws2)
        await broadcast_trace_event(event)
        assert event in ws1.sent
        assert event in ws2.sent

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(manager.disconnect("trace:a", ws1))
        asyncio.run(manager.disconnect("trace:b", ws2))


def test_stream_consumer_read_and_ack():
    fake = _reset()
    from hero_quant.infra.redis import RedisStream

    async def _run():
        stream = RedisStream()
        await stream.publish("hero:stream:trace", {"channel": "trace", "data": "{}"})
        entries = await stream.subscribe_consumer(
            "hero:stream:trace", "hero:ws-trace", "test-consumer", count=10, block=200
        )
        assert entries, "consumer got no entries"
        ids = [mid for mid, _ in entries]
        acked = await stream.ack("hero:stream:trace", "hero:ws-trace", *ids)
        assert acked >= 1
        await fake.flushall()

    asyncio.run(_run())

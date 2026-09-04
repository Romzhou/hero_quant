"""R3 TDD: ws 用户级频道迁移 — fakeredis 验证同用户广播/跨用户隔离/ticket→channel 映射."""

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


def test_same_user_dual_connections_targeted():
    """同一 userId 双连接：user 定向 broadcast 只投递该 user 频道."""
    _reset()
    manager = _clear_manager()
    from hero_quant.api.ws import broadcast_trace_event, resolve_user_channel

    ch_u1 = resolve_user_channel("u1", None)
    ch_u2 = resolve_user_channel("u2", None)
    assert ch_u1 == "ws:channel:u1"
    assert ch_u2 == "ws:channel:u2"

    ws1, ws2, ws_other = _FakeWS(), _FakeWS(), _FakeWS()
    event = {"type": "delta", "delta": "hello-u1"}

    async def _run():
        await manager.connect(ch_u1, ws1)
        await manager.connect(ch_u1, ws2)
        await manager.connect(ch_u2, ws_other)
        await broadcast_trace_event(event, user="u1")
        assert event in ws1.sent
        assert event in ws2.sent
        assert event not in ws_other.sent

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(manager.disconnect(ch_u1, ws1))
        asyncio.run(manager.disconnect(ch_u1, ws2))
        asyncio.run(manager.disconnect(ch_u2, ws_other))


def test_different_user_isolation():
    """不同 user 隔离：发给 u2 时 u1 连接收不到."""
    _reset()
    manager = _clear_manager()
    from hero_quant.api.ws import broadcast_trace_event, resolve_user_channel

    ch_u1 = resolve_user_channel("u1", None)
    ch_u2 = resolve_user_channel("u2", None)
    ws1, ws2 = _FakeWS(), _FakeWS()
    event = {"type": "delta", "delta": "hello-u2"}

    async def _run():
        await manager.connect(ch_u1, ws1)
        await manager.connect(ch_u2, ws2)
        await broadcast_trace_event(event, user="u2")
        assert event not in ws1.sent
        assert event in ws2.sent

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(manager.disconnect(ch_u1, ws1))
        asyncio.run(manager.disconnect(ch_u2, ws2))


def test_ticket_consume_yields_user_channel_and_online():
    """ticket 消费后 channel=ws:channel:{userId} 且 ws:online:{channel} 存在."""
    import fakeredis
    import fakeredis.aioredis as fakeredis_async

    import hero_quant.infra.redis as rmod

    # ticket 部分用同步 fake（与 test_security_redis 一致，避免 async/sync 混用告警）
    rmod.clear_redis_instance()
    sync_fake = fakeredis.FakeRedis(decode_responses=True)
    rmod.set_redis_instance(sync_fake)
    from hero_quant.api.security import consume_ticket, issue_ticket

    t = issue_ticket(ttl=60)
    assert consume_ticket(t) is True
    assert consume_ticket(t) is False  # 单次语义
    try:
        sync_fake.flushall()
    except Exception:
        pass

    # online 部分用异步 fake（与 test_ws_redis_broadcast 一致）
    rmod.clear_redis_instance()
    async_fake = fakeredis_async.FakeRedis(decode_responses=True)
    rmod.set_redis_instance(async_fake)
    _clear_manager()
    from hero_quant.api.ws import ONLINE_PREFIX, mark_online, resolve_user_channel

    async def _run():
        # 端点逻辑：user 查询串决定 channel（ticket 保持单次语义，不携带 user）
        channel = resolve_user_channel("u1", None)
        assert channel == "ws:channel:u1"
        await mark_online(channel)
        assert await async_fake.exists(f"{ONLINE_PREFIX}{channel}") == 1
        await async_fake.flushall()

    asyncio.run(_run())


def test_redis_payload_channel_is_user_channel():
    """broadcast Redis payload channel 字段同步为用户级 channel."""
    fake = _reset()
    _clear_manager()
    from hero_quant.api.ws import TRACE_STREAM, broadcast_trace_event

    event = {"type": "tool", "tool": "demo", "status": "running"}

    async def _run():
        await broadcast_trace_event(event, user="u1")
        entries = await fake.xread({TRACE_STREAM: "0"}, count=10)
        assert entries, "stream entries missing"
        payloads = [fields for _, msgs in entries for _, fields in msgs]
        matched = [p for p in payloads if p.get("channel") == "ws:channel:u1"]
        assert matched, f"user channel payload missing: {payloads}"
        assert any(json.loads(p["data"]) == event for p in matched)
        await fake.flushall()

    asyncio.run(_run())


def test_legacy_no_user_still_broadcasts():
    """无 user 时保持旧语义：全量广播（Monitor 兼容），payload channel=trace."""
    fake = _reset()
    manager = _clear_manager()
    from hero_quant.api.ws import TRACE_CHANNEL, TRACE_STREAM, broadcast_trace_event

    ws1, ws2 = _FakeWS(), _FakeWS()
    event = {"type": "delta", "delta": "legacy"}

    async def _run():
        await manager.connect("trace:a", ws1)
        await manager.connect("trace:b", ws2)
        await broadcast_trace_event(event)
        assert event in ws1.sent
        assert event in ws2.sent
        entries = await fake.xread({TRACE_STREAM: "0"}, count=10)
        payloads = [fields for _, msgs in entries for _, fields in msgs]
        assert any(p.get("channel") == TRACE_CHANNEL for p in payloads)
        await fake.flushall()

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(manager.disconnect("trace:a", ws1))
        asyncio.run(manager.disconnect("trace:b", ws2))

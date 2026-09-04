"""Lane A4 TDD：会话/WS通道/trace 12条 fail-closed/原子性/续约修复。

覆盖（每条先跑红再修绿）：
- session：空id共享key、default=str静默stringify、errors=ignore丢坏字节、set/get类型不对称
- ws：query finally无条件清共享channel、mark_offline非原子、presence过期不续约、
  idle pong不续heartbeat、sync广播静默丢事件
- trace：header未校验、双ContextVar分叉、context未token重置+异常泄漏
"""

from __future__ import annotations

import asyncio
import logging
import re
from types import SimpleNamespace

import fakeredis.aioredis as fakeredis_async
import pytest


def _reset_redis():
    """注入 fakeredis，保证测试不依赖真实 Redis。"""
    import hero_quant.infra.redis as rmod

    rmod.clear_redis_instance()
    fake = fakeredis_async.FakeRedis(decode_responses=True)
    rmod.set_redis_instance(fake)
    return fake


def _clear_ws():
    """清空单机 WS 管理器与心跳表，避免用例间污染。"""
    from hero_quant.api import ws as wsmod

    wsmod.manager._connections.clear()
    wsmod.heartbeat._last_active.clear()
    return wsmod


def _cleanup_ws(wsmod, fake=None):
    wsmod.manager._connections.clear()
    wsmod.heartbeat._last_active.clear()
    if fake is not None:
        try:
            asyncio.run(fake.flushall())
        except Exception:
            pass


class _FakeWS:
    """最小 WebSocket 替身：recv_script 元素可为 dict / "timeout" / "disconnect"。"""

    def __init__(self, recv_script=()):
        self.sent: list = []
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


# ── session 4条 ──


def test_a4_session_empty_id_fail_closed():
    """空/空白 session_id 必须拒绝，且不得产生共享 hero:session: 键。"""
    fake = _reset_redis()
    from hero_quant.infra.session import get_session, set_session

    async def _run():
        assert await set_session("", {"a": 1}) is False
        assert await set_session("   ", {"a": 1}) is False
        assert await get_session("") is None
        assert await get_session("   ") is None
        assert await fake.exists("hero:session:") == 0

    asyncio.run(_run())


def test_a4_session_no_silent_stringify():
    """不可 JSON 序列化值不得被 default=str 悄悄转成字符串后返回成功。"""
    _reset_redis()
    from datetime import datetime

    from hero_quant.infra.session import get_session, set_session

    async def _run():
        assert await set_session("a4-dt", {"when": datetime(2026, 1, 1)}) is False
        assert await get_session("a4-dt") is None

    asyncio.run(_run())


def test_a4_session_strict_bytes_decode():
    """坏字节必须严格解码失败走 None，不得 ignore 后吞成错误 dict。"""
    import hero_quant.infra.redis as rmod
    from hero_quant.infra.session import get_session

    class _BadBytes:
        async def get(self, key):
            return b"{\xff}"  # ignore解码会变成 b'{}' 从而误返回 {}

    rmod.clear_redis_instance()
    rmod.set_redis_instance(_BadBytes())
    try:
        assert asyncio.run(get_session("a4-bad")) is None
    finally:
        rmod.clear_redis_instance()


def test_a4_session_dict_only_symmetry():
    """set 只收 dict（get 只返回 dict），list/str/None 写入必须返回 False。"""
    _reset_redis()
    from hero_quant.infra.session import set_session

    async def _run():
        assert await set_session("a4-l", ["x"]) is False
        assert await set_session("a4-s", "str") is False
        assert await set_session("a4-n", None) is False

    asyncio.run(_run())


# ── ws 5条 ──


def test_a4_ws_query_finally_keeps_shared_channel():
    """共享 channel 上仍有兄弟连接时，query 断开不得清心跳/presence。"""
    fake = _reset_redis()
    wsmod = _clear_ws()
    orig = wsmod.consume_ticket
    wsmod.consume_ticket = lambda t: True  # 测试替身
    try:

        async def _run():
            from hero_quant.api.ws import ONLINE_PREFIX

            sibling = _FakeWS()
            main = _FakeWS(recv_script=["disconnect"])
            channel = "ws:channel:a4-shared"
            await wsmod.manager.connect(channel, sibling)
            wsmod.heartbeat.record(channel)
            await wsmod.mark_online(channel)
            await wsmod.ws_query(main, ticket="t", user="a4-shared")
            assert wsmod.manager.is_online(channel) is True
            assert channel in wsmod.heartbeat._last_active
            assert await fake.exists(f"{ONLINE_PREFIX}{channel}") == 1

        asyncio.run(_run())
    finally:
        wsmod.consume_ticket = orig
        _cleanup_ws(wsmod, fake)


def test_a4_ws_mark_offline_atomic():
    """mark_offline 必须原子比较后删除：GET 快照过期、兄弟已重建时不得误删。"""
    import hero_quant.infra.redis as rmod
    from hero_quant.api import ws as wsmod
    from hero_quant.api.ws import INSTANCE_ID, ONLINE_PREFIX

    key = f"{ONLINE_PREFIX}a4-race"
    store = {key: "SIBLING-INSTANCE"}
    calls = {"eval": 0}

    class _RaceRedis:
        async def get(self, k):
            return INSTANCE_ID  # 过期快照：读到的还是自己的旧值

        async def delete(self, k):
            store.pop(k, None)
            return 1

        async def eval(self, script, nkeys, k, *args):
            calls["eval"] += 1
            if store.get(k) == args[0]:
                store.pop(k, None)
                return 1
            return 0

    rmod.clear_redis_instance()
    rmod.set_redis_instance(_RaceRedis())
    try:
        asyncio.run(wsmod.mark_offline("a4-race"))
        assert calls["eval"] >= 1  # 必须走原子路径
        assert store.get(key) == "SIBLING-INSTANCE"  # 兄弟 presence 必须保留
    finally:
        rmod.clear_redis_instance()


def test_a4_ws_idle_pong_extends_heartbeat():
    """idle 超时走 pong 分支后必须续 heartbeat，否则健康空闲连接被误杀。"""
    _reset_redis()
    wsmod = _clear_ws()
    orig_ct = wsmod.consume_ticket
    wsmod.consume_ticket = lambda t: True  # 测试替身
    orig_record = wsmod.heartbeat.record
    calls = {"n": 0}

    def _spy(ch):
        calls["n"] += 1
        return orig_record(ch)

    wsmod.heartbeat.record = _spy
    try:

        async def _run():
            main = _FakeWS(recv_script=["timeout", "disconnect"])
            await wsmod.ws_trace(main, ticket="t", user="")
            assert any(m.get("type") == "pong" for m in main.sent)
            assert calls["n"] >= 2  # 建连1次 + pong续约1次

        asyncio.run(_run())
    finally:
        wsmod.consume_ticket = orig_ct
        wsmod.heartbeat.record = orig_record
        _cleanup_ws(wsmod)


def test_a4_ws_pong_refreshes_presence_ttl():
    """pong 存活必须同步续约 presence TTL，否则 >90s 连接被误判离线。"""
    _reset_redis()
    wsmod = _clear_ws()
    orig_ct = wsmod.consume_ticket
    wsmod.consume_ticket = lambda t: True  # 测试替身
    orig_online = wsmod.mark_online
    calls = {"n": 0}

    async def _spy(ch):
        calls["n"] += 1
        return await orig_online(ch)

    wsmod.mark_online = _spy
    try:

        async def _run():
            main = _FakeWS(recv_script=["timeout", "disconnect"])
            await wsmod.ws_trace(main, ticket="t", user="")
            assert calls["n"] >= 2  # 建连1次 + pong续约1次

        asyncio.run(_run())
    finally:
        wsmod.consume_ticket = orig_ct
        wsmod.mark_online = orig_online
        _cleanup_ws(wsmod)


def test_a4_ws_sync_broadcast_no_loop_logged(caplog):
    """无运行 loop 时 sync 广播不得静默吞事件，至少打一条可观测日志。"""
    wsmod = _clear_ws()
    with caplog.at_level(logging.DEBUG, logger="hero_quant.api.ws"):
        wsmod.broadcast_trace_event_sync({"type": "ping"})
    assert any("no_loop" in (r.getMessage() or "") for r in caplog.records)


def test_a4_ws_sync_broadcast_task_error_logged(caplog):
    """有 loop 时后台任务异常必须被 done-callback 观测到并打日志。"""
    _reset_redis()
    wsmod = _clear_ws()

    async def _boom(event):
        raise RuntimeError("a4-boom")

    orig = wsmod.broadcast_trace_event
    wsmod.broadcast_trace_event = _boom
    try:

        async def _run():
            with caplog.at_level(logging.DEBUG, logger="hero_quant.api.ws"):
                wsmod.broadcast_trace_event_sync({"type": "ping"})
                await asyncio.sleep(0.2)
            assert any("broadcast_sync_failed" in (r.getMessage() or "") for r in caplog.records)

        asyncio.run(_run())
    finally:
        wsmod.broadcast_trace_event = orig
        _cleanup_ws(wsmod)


# ── trace 3条 ──


@pytest.mark.parametrize("evil", ["   ", "bad\nvalue", "a!b@c", "x" * 200])
def test_a4_trace_header_validated(evil):
    """非法 x-request-id（空白/CRLF/非法字符/超长）不得入库与回显，应回退生成。"""
    from starlette.responses import Response

    from hero_quant.api.middleware import trace as tmod
    from hero_quant.api.middleware.trace import TraceIdMiddleware

    async def _call_next(req):
        return Response("ok")

    mw = TraceIdMiddleware(app=None)
    req = SimpleNamespace(headers={"x-request-id": evil})
    try:
        resp = asyncio.run(mw.dispatch(req, _call_next))
        rid = resp.headers["x-request-id"]
        assert rid != evil
        assert re.fullmatch(r"[A-Za-z0-9\-_.:]{1,128}", rid)
        assert tmod.get_trace_id() != evil
    finally:
        tmod.clear_trace_id()


def test_a4_trace_valid_id_passthrough():
    """合法 ID（如 pr3i-trace-123）必须原样透传，不得误杀（回归 guard）。"""
    from starlette.responses import Response

    from hero_quant.api.middleware import trace as tmod
    from hero_quant.api.middleware.trace import TraceIdMiddleware

    async def _call_next(req):
        return Response("ok")

    mw = TraceIdMiddleware(app=None)
    req = SimpleNamespace(headers={"x-request-id": "pr3i-trace-123"})
    try:
        resp = asyncio.run(mw.dispatch(req, _call_next))
        assert resp.headers["x-request-id"] == "pr3i-trace-123"
        assert resp.headers["x-trace-id"] == "pr3i-trace-123"
    finally:
        tmod.clear_trace_id()


def test_a4_trace_set_syncs_both_vars():
    """set_trace_id 必须同步双 ContextVar，且空值回退生成。"""
    from hero_quant.api.middleware import trace as tmod

    try:
        assert tmod.set_trace_id("abc") == "abc"
        assert tmod.get_trace_id() == "abc"
        assert tmod.get_request_id() == "abc"
        v = tmod.set_trace_id("")
        assert v and tmod.get_trace_id() == v == tmod.get_request_id()
        v2 = tmod.set_trace_id(None)
        assert v2 and tmod.get_request_id() == v2
    finally:
        tmod.clear_trace_id()


def test_a4_trace_context_reset_on_success_and_error():
    """dispatch 必须用 token 复位：成功与异常路径都不泄漏到外层上下文。"""
    from starlette.responses import Response

    from hero_quant.api.middleware import trace as tmod
    from hero_quant.api.middleware.trace import TraceIdMiddleware

    async def _ok(req):
        return Response("ok")

    async def _boom(req):
        raise RuntimeError("a4")

    mw = TraceIdMiddleware(app=None)
    req = SimpleNamespace(headers={})
    try:
        tmod.set_trace_id("outer")
        resp = asyncio.run(mw.dispatch(req, _ok))
        assert resp.headers["x-request-id"]
        assert tmod.get_trace_id() == "outer"
        assert tmod.get_request_id() == "outer"
        with pytest.raises(RuntimeError):
            asyncio.run(mw.dispatch(req, _boom))
        assert tmod.get_trace_id() == "outer"
        assert tmod.get_request_id() == "outer"
    finally:
        tmod.clear_trace_id()


# ── 补漏（自 review 三文件时发现，同样 TDD 先跑红） ──


def test_a4_session_id_normalized_and_capped():
    """session_id 前后空白应归一化到同一键；超长 id 直接拒绝（防超大键 DoS）。"""
    fake = _reset_redis()
    from hero_quant.infra.session import get_session, set_session

    async def _run():
        assert await set_session("  a4-pad  ", {"v": 1}) is True
        assert await get_session("a4-pad") == {"v": 1}
        assert await get_session("  a4-pad  ") == {"v": 1}
        long_id = "x" * 300
        assert await set_session(long_id, {"v": 1}) is False
        assert await get_session(long_id) is None
        assert await fake.exists("hero:session:") == 0

    asyncio.run(_run())


def test_a4_ws_presence_guards_empty_channel():
    """presence 三件套遇空 channel 直接返回，不得建键、不得抛错。"""
    fake = _reset_redis()
    wsmod = _clear_ws()

    async def _run():
        await wsmod.mark_online("")
        await wsmod.refresh_presence("   ")
        await wsmod.mark_offline("")
        assert await fake.exists("ws:online:") == 0
        assert await fake.dbsize() == 0  # "ws:online:   " 之类也不许建

    asyncio.run(_run())


def test_a4_ws_long_user_channel_isolated():
    """超长 user 不得静默截断到同一 channel（前64字符相同则跨用户串扰）。"""
    wsmod = _clear_ws()
    a = wsmod.resolve_user_channel("u" * 64 + "A")
    b = wsmod.resolve_user_channel("u" * 64 + "B")
    assert a != b
    assert wsmod.resolve_user_channel("alice") == "ws:channel:alice"  # 短 id 语义不变
    _cleanup_ws(wsmod)


def test_a4_ws_heartbeat_remove_cleans_presence_touch():
    """heartbeat.remove 必须同步清理 presence 续约节流表，否则 _presence_touched 无界增长。"""
    from datetime import datetime, timezone

    wsmod = _clear_ws()
    wsmod._presence_touched["ch-a4"] = datetime.now(timezone.utc)
    wsmod.heartbeat.record("ch-a4")
    wsmod.heartbeat.remove("ch-a4")
    assert "ch-a4" not in wsmod._presence_touched
    _cleanup_ws(wsmod)


def test_a4_ws_send_to_narrow_errors():
    """send_to 只吞协议/IO/序列化异常；未知程序错误必须上抛，不得误判为离线摘除。"""
    wsmod = _clear_ws()

    class _Boom:
        async def send_json(self, data):
            raise AssertionError("a4-program-bug")

    wsmod.manager._connections["ch-a4"].add(_Boom())  # 测试直接布线

    async def _run():
        with pytest.raises(AssertionError):
            await wsmod.manager.send_to("ch-a4", {"type": "ping"})

    try:
        asyncio.run(_run())
    finally:
        _cleanup_ws(wsmod)

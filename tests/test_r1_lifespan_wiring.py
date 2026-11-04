"""R1 TDD: lifespan 接线 — consumer 启动 + middleware 挂载 + state 注入.

断言（TestClient 启动后）:
(a) app.state 有 memory_store/agent 属性（且注入函数被调）；
(b) 中间件栈含 TraceId/SecurityHeaders（遍历 app.user_middleware）；
(c) run_trace_consumer 任务已创建（mock ws.run_trace_consumer 断言被调，
    stop_event 在 shutdown 置位）。
fakeredis 隔离，不依赖真实 Redis。
"""

from unittest.mock import AsyncMock, patch

import fakeredis


def _isolate_redis():
    import hero_quant.infra.redis as rmod

    rmod.clear_redis_instance()
    fake = fakeredis.FakeRedis(decode_responses=True)
    rmod.set_redis_instance(fake)
    return fake


def test_r1_state_injection_on_startup():
    _isolate_redis()
    from fastapi.testclient import TestClient

    import hero_quant.agent.container as container_mod
    import hero_quant.agent.memory.store as store_mod
    import hero_quant.api.ws as ws_mod
    from hero_quant.api.server import app

    with (
        patch.object(store_mod, "inject_session_buffer", wraps=store_mod.inject_session_buffer) as m_mem,
        patch.object(container_mod, "inject_agent_container", wraps=container_mod.inject_agent_container) as m_agent,
        patch.object(ws_mod, "run_trace_consumer", new=AsyncMock()),
    ):
        with TestClient(app):
            assert hasattr(app.state, "memory_store"), "app.state.memory_store missing after startup"
            assert hasattr(app.state, "session_buffer"), "app.state.session_buffer missing after startup"
            assert hasattr(app.state, "agent"), "app.state.agent missing after startup"
            # 同名类陷阱护栏：持久化记忆与会话缓冲必须是不同模块的实例
            assert type(app.state.memory_store).__module__ == "hero_quant.memory.store"
            assert type(app.state.session_buffer).__module__ == "hero_quant.agent.memory.store"
    assert m_mem.called, "inject_session_buffer not called on startup"
    assert m_agent.called, "inject_agent_container not called on startup"


def test_r1_middleware_mounted():
    from hero_quant.api.middleware import SecurityHeadersMiddleware, TraceIdMiddleware
    from hero_quant.api.server import app

    classes = [getattr(m, "cls", None) for m in app.user_middleware]
    assert TraceIdMiddleware in classes, f"TraceIdMiddleware missing: {classes}"
    assert SecurityHeadersMiddleware in classes, f"SecurityHeadersMiddleware missing: {classes}"


def test_r1_trace_consumer_start_stop():
    _isolate_redis()
    from fastapi.testclient import TestClient

    import hero_quant.api.ws as ws_mod
    from hero_quant.api.server import app

    with patch.object(ws_mod, "run_trace_consumer", new=AsyncMock()) as m_consumer:
        with TestClient(app):
            assert m_consumer.called, "run_trace_consumer not started on startup"
            args = m_consumer.call_args.args
            kwargs = m_consumer.call_args.kwargs
            stop = args[0] if args else kwargs.get("stop_event")
            assert stop is not None, "run_trace_consumer called without stop_event"
            assert not stop.is_set(), "stop_event should not be set while running"
        assert stop.is_set(), "stop_event not set on shutdown"

"""P0-3 端到端：持久记忆装配 + 跨进程写入可见。

缺陷本体（P0-3）：agent/memory/store.py::SessionBuffer（进程内 LRU）与
memory/store.py::MemoryStore（文件 + SQLite FTS5）曾同名；server 注入的是前者，
两处 loop_kwargs 都没传 memory_store，持久记忆从不落盘。

三层接线（AGENTS §11）：
- 层1 非 None：真实 app 启动后 app.state.memory_store / session_buffer 存在；
- 层2 精确类型：type(x).__module__ 用模块全路径，挡同名陷阱；
- 层3 行为探针：写穿到磁盘，另一个 MemoryStore 实例（模拟新进程）能 recall 出来；
  再经真实 /v1/query（无 API key → _FakeLLM）证明 server 的 loop_kwargs 真的把
  持久化 store 传进了 AgentLoop（这条不 mock 任何内部实现）。
"""

from __future__ import annotations

import uuid

_TOKEN = "zzqp0memorytoken"


def _isolated_app(monkeypatch, tmp_path):
    """真实装配：env 指向临时数据根，清 settings 缓存后由 TestClient 跑完 lifespan。"""
    monkeypatch.setenv("HERO_DATA_DIR", str(tmp_path))
    for key in ("HERO_API_KEY", "OPENAI_API_KEY", "HERO_LLM_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    from hero_quant.config.settings import get_settings

    get_settings.cache_clear()
    from fastapi.testclient import TestClient

    from hero_quant.api import server as srv

    return srv.app, TestClient(srv.app, client=("p0-memory", 50000))


def test_app_state_holds_persistent_memory_store_not_session_buffer(monkeypatch, tmp_path):
    """层1 + 层2：memory_store 必须是持久化实现，SessionBuffer 另挂 session_buffer。"""
    app, client = _isolated_app(monkeypatch, tmp_path)
    with client:
        ms = app.state.memory_store
        sb = app.state.session_buffer
        assert type(ms).__module__ == "hero_quant.memory.store"
        assert type(ms).__name__ == "MemoryStore"
        assert type(sb).__module__ == "hero_quant.agent.memory.store"
        assert type(sb).__name__ == "SessionBuffer"


def test_write_is_visible_to_a_new_process_instance(monkeypatch, tmp_path):
    """层3：进程 A 写入，新 MemoryStore 实例（新进程/新请求）recall 得出。"""
    app, client = _isolated_app(monkeypatch, tmp_path)
    marker = "zzq" + uuid.uuid4().hex[:12]
    with client:
        content = f"{_TOKEN} 持久记忆写入探针 {marker}"
        app.state.memory_store.write("p0-memory-key", content)

    from hero_quant.memory.store import MemoryStore

    fresh = MemoryStore(base_path=tmp_path / "memory")
    # 用唯一 marker 检索：Redis L2 是全局 query 级缓存，共用 token 会让其它用例的
    # 缓存命中掩盖「本实例其实没写进去」，那样测试就假绿了。
    hits = fresh.recall(marker)
    assert hits, "新实例 recall 不到进程 A 的写入（进程内缓存而非持久化）"
    assert any(marker in str(h.get("content") or h.get("text") or "") for h in hits)
    fresh.close()


def test_real_query_endpoint_wires_store_into_agent_loop(monkeypatch, tmp_path):
    """端到端：真实 /v1/query 的 loop_kwargs 用持久化 store，答案被 writeback 落盘。"""
    app, client = _isolated_app(monkeypatch, tmp_path)
    from hero_quant.api import security

    marker = "zzq" + uuid.uuid4().hex[:12]
    goal = f"{_TOKEN} {marker} 请回答"
    with client:
        resp = client.get("/v1/query", params={"q": goal}, headers={"X-Ticket": security.issue_ticket(ttl=60)})
        assert resp.status_code == 200, resp.text

    from hero_quant.memory.store import MemoryStore

    fresh = MemoryStore(base_path=tmp_path / "memory")
    hits = fresh.recall(marker)
    assert hits, "loop_kwargs 没接持久化 store：query 结束后没有 writeback"
    assert any(marker in str(h.get("content") or h.get("text") or "") for h in hits)
    fresh.close()

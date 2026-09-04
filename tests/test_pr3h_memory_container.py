"""PR3-H: MemoryStore LRU + MemoryBuffer 截断 + AgentContainer 幂等回归."""

from hero_quant.agent.memory.buffer import MemoryBuffer
from hero_quant.agent.memory.store import MAX_SESSIONS, MemoryStore
from hero_quant.agent.container import AgentContainer, AgentState


def test_memory_store_evicts_oldest_over_limit():
    store = MemoryStore()
    assert MAX_SESSIONS == 1000
    for sid in range(1001):
        store.get_buffer(sid)
    assert store.size == 1000
    assert not store.has_buffer(0)
    assert store.has_buffer(1000)


def test_memory_store_lru_touch_keeps_hot_session():
    store = MemoryStore()
    for sid in range(1000):
        store.get_buffer(sid)
    # touch session 0 -> becomes most-recently-used
    store.get_buffer(0)
    store.get_buffer(1000)
    assert store.size == 1000
    assert store.has_buffer(0)
    assert not store.has_buffer(1)


def test_memory_buffer_truncates_to_40_messages():
    buf = MemoryBuffer(max_turns=20)
    for i in range(30):
        buf.add_user_message(f"u{i}")
        buf.add_assistant_message(f"a{i}")
    msgs = buf.get_messages()
    assert len(msgs) == 40
    # oldest two rounds evicted by deque(maxlen=40)
    assert msgs[0].content == "u10"
    assert msgs[-1].content == "a29"


def test_agent_container_double_init_idempotent():
    c = AgentContainer()
    g1 = c.init_graph()
    g2 = c.init_graph()
    assert g1 is g2


def test_agent_state_default_max_iterations():
    s = AgentState(user_input="hi", session_id=1, user_id=2)
    assert s.max_iterations == 5
    assert s.iterations == 0


def test_loop_duck_holds_buffer_store():
    from hero_quant.agent.loop import AgentLoop

    class FakeLLM:
        def stream_chat(self, goal):
            return [{"type": "text", "text": "ok"}]

    store = MemoryStore()
    loop = AgentLoop(llm=FakeLLM(), memory_store=store)
    # duck-type: loop holds the new buffer-style store without AttributeError
    assert loop.memory_store is store
    buf = loop.memory_store.get_buffer(7)
    assert isinstance(buf, MemoryBuffer)

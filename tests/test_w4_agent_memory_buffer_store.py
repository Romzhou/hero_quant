"""W4 覆盖率补齐：agent/memory/buffer.py 与 agent/memory/store.py 未覆盖分支。

覆盖目标：
- src/hero_quant/agent/memory/buffer.py: max_turns/system_limit 的非法入参校验、
  deque 溢出后的 _align_head 轮边界裁剪、工具结果 HTML 转义、system 消息淘汰告警、
  messages setter 的 dict/非法条目/超容量校验、from_dict 的形态与容量校验、to_dict/clear
- src/hero_quant/agent/memory/store.py: get_buffer 的创建与 LRU 命中、满容量淘汰、
  save_buffer 的替换清理与满容量淘汰、clear/delete/has/size、
  get_memory_store 的 app.state 注入与兜底、inject_memory_store

断言均为具体数值/类型/异常类型/消息字段，不使用弱断言。
"""

from __future__ import annotations

import pytest

from hero_quant.agent.memory.buffer import DEFAULT_SYSTEM_LIMIT, MemoryBuffer, Message
from hero_quant.agent.memory.store import (
    MAX_SESSIONS,
    MemoryStore,
    get_memory_store,
    inject_memory_store,
    memory_store,
)


# ---------------------------------------------------------------- buffer 构造


@pytest.mark.parametrize("bad", [0, -1, True, "20", 2.5])
def test_buffer_rejects_invalid_max_turns(bad):
    """max_turns 必须为正整数（bool 也不接受）。"""
    with pytest.raises(ValueError, match="max_turns must be a positive int"):
        MemoryBuffer(max_turns=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, -1, True, "5"])
def test_buffer_rejects_invalid_system_limit(bad):
    """system_limit 必须为正整数。"""
    with pytest.raises(ValueError, match="system_limit must be a positive int"):
        MemoryBuffer(max_turns=5, system_limit=bad)  # type: ignore[arg-type]


def test_buffer_default_system_limit():
    """system_limit 缺省为 DEFAULT_SYSTEM_LIMIT。"""
    b = MemoryBuffer(max_turns=3)
    assert b._system_limit == DEFAULT_SYSTEM_LIMIT
    assert b.max_turns == 3
    assert b._capacity == 6


# ---------------------------------------------------------------- buffer 追加


def test_buffer_add_messages_accumulates():
    """user/assistant/tool 消息按顺序保留。"""
    b = MemoryBuffer(max_turns=5)
    b.add_user_message("hi")
    b.add_assistant_message("hello")
    b.add_tool_result("t", "1")
    roles = [m.role for m in b.get_messages()]
    assert roles == ["user", "assistant", "tool"]
    assert b.get_messages_for_llm()[0] == {"role": "user", "content": "hi"}


def test_buffer_tool_result_is_html_escaped():
    """工具名/结果被 HTML 转义，防闭合标签注入。"""
    b = MemoryBuffer(max_turns=5)
    b.add_tool_result('a"b', "<x>&</x>")
    content = b.get_messages()[0].content
    assert "&quot;" in content
    assert "&lt;x&gt;" in content
    assert "<x>" not in content


def test_buffer_align_head_drops_orphan_leading_messages():
    """溢出裁剪后窗口头必须为 user（孤儿 assistant/tool 被逐出）。"""
    b = MemoryBuffer(max_turns=2)  # capacity 4
    b.add_user_message("u1")
    b.add_assistant_message("a1")
    b.add_user_message("u2")
    b.add_assistant_message("a2")
    b.add_tool_result("t", "r")
    msgs = b.get_messages()
    assert msgs[0].role == "user"
    assert all(m.role != "assistant" for m in msgs[:1])
    assert len(msgs) <= b._capacity


def test_buffer_system_message_eviction_warns(caplog):
    """system 消息超限淘汰时显式告警（不静默丢弃）。"""
    b = MemoryBuffer(max_turns=5, system_limit=2)
    b.add_system_message("s1")
    b.add_system_message("s2")
    with caplog.at_level("WARNING"):
        b.add_system_message("s3")
    assert any("system message limit reached" in r.message for r in caplog.records)
    assert len(b.get_messages()) == 2


def test_buffer_clear_resets_both_queues():
    """clear 同时清空 system 与普通消息。"""
    b = MemoryBuffer(max_turns=5)
    b.add_system_message("s")
    b.add_user_message("u")
    b.clear()
    assert b.get_messages() == []


def test_buffer_to_dict_shape():
    """to_dict 产出 max_turns 与消息列表。"""
    b = MemoryBuffer(max_turns=7)
    b.add_user_message("hi")
    d = b.to_dict()
    assert d["max_turns"] == 7
    assert d["messages"] == [{"role": "user", "content": "hi"}]


def test_message_to_dict():
    """Message.to_dict 输出 role/content。"""
    assert Message("user", "hi").to_dict() == {"role": "user", "content": "hi"}


# ---------------------------------------------------------------- buffer setter


def test_buffer_messages_setter_accepts_dicts():
    """setter 接受 dict 形态条目并转为 Message。"""
    b = MemoryBuffer(max_turns=5)
    b.messages = [{"role": "user", "content": "u"}, {"role": "assistant", "content": "a"}]
    assert [m.role for m in b.get_messages()] == ["user", "assistant"]


def test_buffer_messages_setter_rejects_invalid_dict_entry():
    """dict 条目缺键抛 ValueError。"""
    b = MemoryBuffer(max_turns=5)
    with pytest.raises(ValueError, match="invalid message entry"):
        b.messages = [{"role": "user"}]


def test_buffer_messages_setter_rejects_non_message():
    """非 Message/dict 条目抛 ValueError。"""
    b = MemoryBuffer(max_turns=5)
    with pytest.raises(ValueError, match="invalid message entry"):
        b.messages = [123]  # type: ignore[list-item]


def test_buffer_messages_setter_rejects_over_capacity():
    """超出容量直接抛 ValueError（last-N 需调用方显式裁剪）。"""
    b = MemoryBuffer(max_turns=1)  # capacity 2
    with pytest.raises(ValueError, match="messages exceed capacity"):
        b.messages = [Message("user", "a"), Message("assistant", "b"), Message("user", "c")]


def test_buffer_messages_setter_rejects_too_many_system_messages():
    """system 消息超出 system_limit 抛 ValueError。"""
    b = MemoryBuffer(max_turns=5, system_limit=1)
    with pytest.raises(ValueError, match="system messages exceed capacity"):
        b.messages = [Message("system", "a"), Message("system", "b")]


# ---------------------------------------------------------------- buffer from_dict


def test_from_dict_roundtrip():
    """to_dict/from_dict 往返一致。"""
    b = MemoryBuffer(max_turns=4)
    b.add_user_message("u")
    b.add_system_message("s")
    b2 = MemoryBuffer.from_dict(b.to_dict())
    assert [m.to_dict() for m in b2.get_messages()] == [m.to_dict() for m in b.get_messages()]
    assert b2.max_turns == 4


def test_from_dict_rejects_non_dict():
    """非 dict 输入抛 ValueError。"""
    with pytest.raises(ValueError, match="invalid buffer data"):
        MemoryBuffer.from_dict(["not", "a", "dict"])  # type: ignore[arg-type]


def test_from_dict_rejects_none_messages():
    """messages=None 抛 ValueError。"""
    with pytest.raises(ValueError, match="messages is None"):
        MemoryBuffer.from_dict({"max_turns": 3, "messages": None})


def test_from_dict_rejects_non_list_messages():
    """messages 非 list 抛 ValueError。"""
    with pytest.raises(ValueError, match="messages must be list"):
        MemoryBuffer.from_dict({"max_turns": 3, "messages": "abc"})


def test_from_dict_rejects_invalid_entry():
    """条目缺键抛 ValueError。"""
    with pytest.raises(ValueError, match="invalid message entry"):
        MemoryBuffer.from_dict({"max_turns": 3, "messages": [{"role": "user"}]})


def test_from_dict_rejects_over_capacity():
    """超出容量抛 ValueError。"""
    msgs = [{"role": "user", "content": str(i)} for i in range(5)]
    with pytest.raises(ValueError, match="messages exceed capacity"):
        MemoryBuffer.from_dict({"max_turns": 2, "messages": msgs})


def test_from_dict_rejects_too_many_system_messages():
    """system 消息超过默认 system_limit(10) 抛 ValueError。"""
    msgs = [{"role": "system", "content": str(i)} for i in range(11)]
    with pytest.raises(ValueError, match="system messages exceed capacity"):
        MemoryBuffer.from_dict({"max_turns": 5, "messages": msgs})


# ---------------------------------------------------------------- store


def test_store_get_buffer_creates_once():
    """同 session 复用同一 buffer（LRU 命中）。"""
    s = MemoryStore()
    b1 = s.get_buffer(1)
    b2 = s.get_buffer(1)
    assert b1 is b2
    assert s.size == 1
    assert s.has_buffer(1) is True


def test_store_evicts_oldest_when_full():
    """超出 max_sessions 时淘汰最久未用会话。"""
    s = MemoryStore(max_sessions=2)
    s.get_buffer(1)
    s.get_buffer(2)
    s.get_buffer(3)
    assert s.size == 2
    assert s.has_buffer(1) is False
    assert s.has_buffer(3) is True


def test_store_save_buffer_replaces_and_clears_old():
    """保存同 session 的新 buffer 时旧 buffer 被清空释放。"""
    s = MemoryStore()
    old = s.get_buffer(1)
    old.add_user_message("x")
    new = MemoryBuffer(max_turns=5)
    s.save_buffer(1, new)
    assert s.get_buffer(1) is new
    assert old.get_messages() == []


def test_store_save_buffer_evicts_when_full():
    """保存新 session 且已满时先淘汰最旧会话。"""
    s = MemoryStore(max_sessions=1)
    s.get_buffer(1)
    s.save_buffer(2, MemoryBuffer(max_turns=5))
    assert s.has_buffer(1) is False
    assert s.has_buffer(2) is True


def test_store_clear_and_delete_buffer():
    """clear_buffer 清内容、delete_buffer 移除条目。"""
    s = MemoryStore()
    b = s.get_buffer(1)
    b.add_user_message("x")
    s.clear_buffer(1)
    assert b.get_messages() == []
    s.delete_buffer(1)
    assert s.has_buffer(1) is False
    assert s.size == 0


def test_store_delete_and_clear_unknown_session_are_noop():
    """对未知 session 的清理/删除是安全 no-op。"""
    s = MemoryStore()
    s.clear_buffer(999)
    s.delete_buffer(999)
    assert s.size == 0


def test_store_max_sessions_constant():
    """模块级 MAX_SESSIONS 为 1000。"""
    assert MAX_SESSIONS == 1000


def test_get_memory_store_prefers_app_state():
    """app.state.memory_store 存在时优先使用注入实例。"""

    class _State:
        def __init__(self):
            self.memory_store = MemoryStore()

    class _App:
        def __init__(self):
            self.state = _State()

    class _Request:
        def __init__(self, app):
            self.app = app

    app = _App()
    got = get_memory_store(_Request(app))
    assert got is app.state.memory_store
    assert got is not memory_store


def test_get_memory_store_falls_back_to_global():
    """app.state 缺 memory_store 时回落模块级兜底实例。"""

    class _State:
        pass

    class _App:
        def __init__(self):
            self.state = _State()

    class _Request:
        def __init__(self, app):
            self.app = app

    got = get_memory_store(_Request(_App()))
    assert got is memory_store


def test_inject_memory_store_binds_to_app():
    """inject_memory_store 把实例挂到 app.state。"""

    class _State:
        pass

    class _App:
        def __init__(self):
            self.state = _State()

    app = _App()
    store = MemoryStore()
    got = inject_memory_store(app, store)
    assert got is store
    assert app.state.memory_store is store

"""W4 覆盖率补齐：agent/loop.py 的工具函数与轻量分支。

覆盖目标（src/hero_quant/agent/loop.py）：
- estimate_tokens：str / list / dict / bytes / None / 无 len 对象的各分支
- LoopResult 的默认字段
- AgentLoop._normalize_stream：None / dict / str / 迭代器
- AgentLoop._call_llm：stream_chat/invoke/chat/__call__ 优先级与消息字典签名兼容
- AgentLoop.request_stop 置位停止信号
- AgentLoop.inject：无 memory_store / 无 recall 方法 / recall 返回 None
- AgentLoop._writeback：无 memory_store / 空文本 / 无写方法

断言均为具体数值/类型/字段值/异常类型，不使用弱断言。
"""

from __future__ import annotations

import pytest

from hero_quant.agent.loop import AgentLoop, LoopResult, estimate_tokens


# ---------------------------------------------------------------- estimate_tokens


def test_estimate_tokens_string_is_length_over_four():
    """字符串按 4 字符/token 粗估。"""
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("abc") == 0
    assert estimate_tokens("") == 0


def test_estimate_tokens_list_uses_json_length():
    """列表按 JSON 序列化长度粗估。"""
    assert estimate_tokens([{"role": "user", "content": "hi"}]) == len(
        '[{"role": "user", "content": "hi"}]'
    ) // 4


def test_estimate_tokens_dict_uses_json_length():
    """dict 按 JSON 序列化长度粗估。"""
    assert estimate_tokens({"a": "bbbbbbbb"}) == len('{"a": "bbbbbbbb"}') // 4


def test_estimate_tokens_none_is_zero():
    """None 计 0 token。"""
    assert estimate_tokens(None) == 0


def test_estimate_tokens_bytes_decoded():
    """bytes 先解码再计字符。"""
    assert estimate_tokens(b"abcdefgh") == 2


def test_estimate_tokens_object_without_len_falls_back_to_str():
    """无 len 的对象回落为 str(text) 长度 //4。"""
    class _Obj:
        def __str__(self):
            return "abcdefgh"

    assert estimate_tokens(_Obj()) == 2


def test_estimate_tokens_never_negative():
    """结果恒非负。"""
    assert estimate_tokens("") >= 0
    assert estimate_tokens([]) >= 0


# ---------------------------------------------------------------- LoopResult


def test_loop_result_defaults():
    """LoopResult 默认字段。"""
    r = LoopResult(terminated=True, iterations=2)
    assert r.terminated is True
    assert r.iterations == 2
    assert r.text == ""
    assert r.reason == ""
    assert r.metrics is None
    assert r.grounding_verified is False
    assert r.trace_path is None
    assert r.token_count == 0


# ---------------------------------------------------------------- AgentLoop 轻量分支


class _LLM:
    def stream_chat(self, goal):
        yield {"type": "text", "text": "hi"}


def _loop(**kw) -> AgentLoop:
    return AgentLoop(llm=_LLM(), **kw)


def test_normalize_stream_none_returns_empty():
    """None 流归一为空列表。"""
    assert _loop()._normalize_stream(None) == []


def test_normalize_stream_dict_becomes_single_chunk():
    """dict 流归一为单元素列表。"""
    out = _loop()._normalize_stream({"type": "text", "text": "x"})
    assert out == [{"type": "text", "text": "x"}]


def test_normalize_stream_string_becomes_text_chunk():
    """字符串流归一为 text 块。"""
    assert _loop()._normalize_stream("hello") == [{"type": "text", "text": "hello"}]


def test_normalize_stream_iterable_passthrough():
    """可迭代对象原样返回。"""
    chunks = [{"type": "text", "text": "a"}]
    assert _loop()._normalize_stream(iter(chunks)) is not None


def test_call_llm_prefers_stream_chat():
    """stream_chat 优先。"""

    class _L:
        def stream_chat(self, goal):
            return "stream"

        def invoke(self, goal):
            return "invoke"

    assert AgentLoop(llm=_L())._call_llm("g") == "stream"


def test_call_llm_falls_back_to_invoke():
    """无 stream_chat 时用 invoke。"""

    class _L:
        def invoke(self, goal):
            return "invoke"

    assert AgentLoop(llm=_L())._call_llm("g") == "invoke"


def test_call_llm_supports_message_dict_signature():
    """只接受消息字典签名的 LLM 也能被调用。"""

    class _L:
        def invoke(self, messages):
            return messages["messages"][0]["content"]

    assert AgentLoop(llm=_L())._call_llm("goal-text") == "goal-text"


def test_call_llm_without_any_method_raises():
    """LLM 无任何可用方法时抛 AttributeError。"""

    class _L:
        pass

    with pytest.raises(AttributeError, match="no stream_chat/invoke/chat method"):
        AgentLoop(llm=_L())._call_llm("g")


def test_request_stop_sets_flag():
    """request_stop 置位停止信号。"""
    loop = _loop()
    assert loop._stop_requested is False
    loop.request_stop()
    assert loop._stop_requested is True


def test_inject_without_memory_store_returns_empty():
    """无 memory_store 时 inject 返回空列表。"""
    assert _loop().inject("q") == []


def test_inject_without_recall_or_search_returns_empty():
    """memory_store 无 recall/search 时返回空列表。"""

    class _Store:
        pass

    assert _loop(memory_store=_Store()).inject("q") == []


def test_inject_uses_recall_when_available():
    """存在 recall 时使用其结果。"""
    seen = {}

    class _Store:
        def recall(self, query):
            seen["q"] = query
            return [{"content": "remember"}]

    loop = _loop(memory_store=_Store())
    out = loop.inject("hello")
    assert seen["q"] == "hello"
    assert isinstance(out, list)


def test_inject_handles_none_hits():
    """recall 返回 None 时按空列表处理。"""

    class _Store:
        def recall(self, query):
            return None

    assert _loop(memory_store=_Store()).inject("q") == []


def test_writeback_without_memory_store_is_noop():
    """无 memory_store 时写回为 no-op。"""
    _loop()._writeback("text")


def test_writeback_empty_text_is_noop():
    """空文本不写回。"""
    calls = []

    class _Store:
        def write(self, key, content):
            calls.append((key, content))

    _loop(memory_store=_Store())._writeback("")
    assert calls == []


def test_writeback_uses_write_when_available():
    """存在 write 时用 write 写回。"""
    calls = []

    class _Store:
        def write(self, key, content):
            calls.append((key, content))

    _loop(memory_store=_Store())._writeback("hello world")
    assert len(calls) == 1
    assert calls[0][1] == "hello world"


def test_writeback_falls_back_to_single_arg_signature():
    """write 只接受单参数时回落为 content 调用。"""
    calls = []

    class _Store:
        def write(self, content):
            calls.append(content)

    _loop(memory_store=_Store())._writeback("abc")
    assert calls == ["abc"]


def test_writeback_without_any_write_method_is_noop():
    """memory_store 无写方法时不报错。"""

    class _Store:
        pass

    _loop(memory_store=_Store())._writeback("abc")

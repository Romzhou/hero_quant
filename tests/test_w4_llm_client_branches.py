"""W4 覆盖率补齐：llm/client.py 未覆盖分支。

覆盖目标（src/hero_quant/llm/client.py）：
- _accepts_timeout：显式 timeout 形参 / **kwargs / 无 timeout / 无法静态判定
- _call_with_optional_timeout：带/不带 timeout 的调用与"签名撒谎"的 TypeError 透出
- _stream_with_chat：legacy stream_chat、LangChain .stream 的 str/dict/消息对象/tool_call 形态、
  .invoke 回落、无任何可流式接口时 AttributeError
- stream_chat：首产出前重试、重试耗尽、部分产出后失败不再重试
- _capture_usage：usage / last_usage / 无用量三种形态
- invoke：invoke/chat/__call__/stream_chat 四条路径与全部缺失时的 AttributeError
- chat / __call__ 转发

断言均为具体类型/字段值/异常类型，不使用弱断言。
"""

from __future__ import annotations

import pytest

from hero_quant.llm.client import LLMClient


# ---------------------------------------------------------------- 签名探测


def test_accepts_timeout_detects_timeout_kwarg():
    """显式 timeout 形参被识别。"""

    def _fn(prompt, timeout=None):
        return prompt

    assert LLMClient._accepts_timeout(_fn) is True


def test_accepts_timeout_detects_var_keyword():
    """带 **kwargs 的函数视为支持 timeout。"""

    def _fn(prompt, **kwargs):
        return prompt

    assert LLMClient._accepts_timeout(_fn) is True


def test_accepts_timeout_false_when_absent():
    """无 timeout 形参且无 **kwargs 时判定为不支持。"""

    def _fn(prompt):
        return prompt

    assert LLMClient._accepts_timeout(_fn) is False


def test_accepts_timeout_true_for_uninspectable():
    """无法静态判定（无签名的 builtin）时仍尝试透传 timeout。"""
    import time as _time

    assert LLMClient._accepts_timeout(_time.time) is True


def test_call_with_optional_timeout_without_timeout_support():
    """后端不支持 timeout 时仅传 prompt（不试探重发）。"""
    seen = []

    def _fn(prompt):
        seen.append(prompt)
        return "ok"

    assert LLMClient._call_with_optional_timeout(_fn, "p", 30) == "ok"
    assert seen == ["p"]


def test_call_with_optional_timeout_passes_timeout():
    """后端支持 timeout 时透传 timeout 值。"""
    seen = {}

    def _fn(prompt, timeout=None):
        seen["timeout"] = timeout
        return "ok"

    assert LLMClient._call_with_optional_timeout(_fn, "p", 11) == "ok"
    assert seen["timeout"] == 11


def test_call_with_optional_timeout_type_error_is_not_retried():
    """签名探测称支持但执行期 TypeError 时直接透出，不重发（避免双执行 RPC）。"""
    calls = []

    def _fn(prompt, timeout=None):
        calls.append(prompt)
        raise TypeError("boom")

    with pytest.raises(TypeError, match="rejected timeout kwarg despite signature probe"):
        LLMClient._call_with_optional_timeout(_fn, "p", 5)
    assert len(calls) == 1


# ---------------------------------------------------------------- 流式分派


class _Msg:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        if tool_calls is not None:
            self.tool_calls = tool_calls


def test_stream_prefers_legacy_stream_chat():
    """legacy stream_chat 优先，直接透出原始块。"""

    class _Chat:
        def stream_chat(self, prompt, timeout=None):
            yield "raw-1"
            yield "raw-2"

    c = LLMClient(_Chat())
    assert list(c.stream_chat("hi")) == ["raw-1", "raw-2"]


def test_stream_normalizes_langchain_str_chunk():
    """LangChain .stream 的字符串块归一为 text 事件。"""

    class _Chat:
        def stream(self, prompt, timeout=None):
            yield "hello"

    c = LLMClient(_Chat())
    assert list(c.stream_chat("hi")) == [{"type": "text", "text": "hello"}]


def test_stream_passes_through_dict_chunk():
    """字典块原样透传。"""

    class _Chat:
        def stream(self, prompt, timeout=None):
            yield {"type": "custom", "text": "x"}

    c = LLMClient(_Chat())
    assert list(c.stream_chat("hi")) == [{"type": "custom", "text": "x"}]


def test_stream_normalizes_message_with_content():
    """带 .content 的消息对象归一为 text 事件。"""

    class _Chat:
        def stream(self, prompt, timeout=None):
            yield _Msg(content="answer")

    c = LLMClient(_Chat())
    assert list(c.stream_chat("hi")) == [{"type": "text", "text": "answer"}]


def test_stream_emits_tool_call_chunk():
    """带 tool_calls 的消息块产出 tool_call 事件。"""

    class _Chat:
        def stream(self, prompt, timeout=None):
            yield _Msg(content="calling", tool_calls=[{"name": "t"}])

    c = LLMClient(_Chat())
    out = list(c.stream_chat("hi"))
    assert out[0]["type"] == "tool_call"
    assert out[0]["tool_calls"] == [{"name": "t"}]


def test_stream_emits_tool_call_chunk_without_content():
    """content 为空但有 tool_calls 时仍产出 tool_call 事件（text 为空串）。"""

    class _Chat:
        def stream(self, prompt, timeout=None):
            yield _Msg(content="", tool_calls=[{"name": "t"}])

    c = LLMClient(_Chat())
    out = list(c.stream_chat("hi"))
    assert out == [{"type": "tool_call", "tool_calls": [{"name": "t"}], "text": ""}]


def test_stream_stringifies_unknown_chunk():
    """未知类型块被字符串化后作为 text 产出。"""

    class _Chat:
        def stream(self, prompt, timeout=None):
            yield 42

    c = LLMClient(_Chat())
    assert list(c.stream_chat("hi")) == [{"type": "text", "text": "42"}]


def test_stream_falls_back_to_invoke():
    """无 stream/stream_chat 时回落 invoke，产出单块 text。"""

    class _Chat:
        def invoke(self, prompt, timeout=None):
            return _Msg(content="only-invoke")

    c = LLMClient(_Chat())
    assert list(c.stream_chat("hi")) == [{"type": "text", "text": "only-invoke"}]


def test_stream_invoke_fallback_stringifies_non_text_result():
    """invoke 返回非字符串且无 content 时用 str() 兜底。"""

    class _Chat:
        def invoke(self, prompt, timeout=None):
            return 12345

    c = LLMClient(_Chat())
    assert list(c.stream_chat("hi")) == [{"type": "text", "text": "12345"}]


def test_stream_without_any_streamable_interface_raises():
    """底层 chat 无任何可流式接口时抛 AttributeError。"""

    class _Chat:
        pass

    c = LLMClient(_Chat())
    with pytest.raises(AttributeError, match="no stream_chat/stream/invoke"):
        list(c.stream_chat("hi"))


# ---------------------------------------------------------------- 重试语义


def test_stream_retries_before_first_chunk(monkeypatch):
    """首块产出前的瞬时错误会重试并最终成功。"""
    import hero_quant.llm.client as client_mod

    monkeypatch.setattr(client_mod.time, "sleep", lambda _d: None)
    calls = []

    class _Chat:
        def stream_chat(self, prompt, timeout=None):
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("flaky")
            yield "done"

    c = LLMClient(_Chat(), max_retries=3)
    assert list(c.stream_chat("hi")) == ["done"]
    assert len(calls) == 3


def test_stream_raises_after_retries_exhausted(monkeypatch):
    """重试耗尽后原始异常透出。"""
    import hero_quant.llm.client as client_mod

    monkeypatch.setattr(client_mod.time, "sleep", lambda _d: None)
    calls = []

    class _Chat:
        def stream_chat(self, prompt, timeout=None):
            calls.append(1)
            raise ConnectionError("always")

    c = LLMClient(_Chat(), max_retries=2)
    with pytest.raises(ConnectionError):
        list(c.stream_chat("hi"))
    assert len(calls) == 3


# ---------------------------------------------------------------- usage 采集


def test_capture_usage_prefers_usage_attribute():
    """优先采集 chat.usage。"""

    class _Chat:
        usage = {"prompt_tokens": 7, "completion_tokens": 3}

    c = LLMClient(_Chat())
    c._capture_usage()
    assert c.usage == {"prompt_tokens": 7, "completion_tokens": 3}
    assert c.last_usage == {"prompt_tokens": 7, "completion_tokens": 3}


def test_capture_usage_falls_back_to_last_usage():
    """无 usage 时采集 last_usage。"""

    class _Chat:
        last_usage = {"prompt_tokens": 1}

    c = LLMClient(_Chat())
    c._capture_usage()
    assert c.usage == {"prompt_tokens": 1}


def test_capture_usage_absent_leaves_none():
    """后端不提供用量时保持 None（无陈旧计数）。"""

    class _Chat:
        pass

    c = LLMClient(_Chat())
    c._capture_usage()
    assert c.usage is None
    assert c.last_usage is None


def test_reset_usage_clears_state():
    """每次调用前重置用量，避免跨调用串味。"""

    class _Chat:
        usage = {"prompt_tokens": 5}

    c = LLMClient(_Chat())
    c._capture_usage()
    assert c.usage is not None
    c._reset_usage()
    assert c.usage is None
    assert c.last_usage is None


# ---------------------------------------------------------------- invoke 分派


def test_invoke_uses_invoke_method():
    """有 invoke 时优先走 invoke。"""

    class _Chat:
        def invoke(self, prompt, timeout=None):
            return "from-invoke"

    c = LLMClient(_Chat())
    assert c.invoke("hi") == "from-invoke"


def test_invoke_uses_chat_method_when_no_invoke():
    """无 invoke 但有 chat 时走 chat。"""

    class _Chat:
        def chat(self, prompt, timeout=None):
            return "from-chat"

    c = LLMClient(_Chat())
    assert c.invoke("hi") == "from-chat"


def test_invoke_uses_callable_object():
    """既无 invoke/chat 但对象可调用时走 __call__。"""

    class _Chat:
        def __call__(self, prompt, timeout=None):
            return "from-call"

    c = LLMClient(_Chat())
    assert c.invoke("hi") == "from-call"


def test_invoke_joins_stream_chat_output():
    """仅有 stream_chat 时把分块文本拼接返回。"""

    class _Chat:
        def stream_chat(self, prompt, timeout=None):
            yield "a"
            yield {"type": "text", "text": "b"}

    c = LLMClient(_Chat())
    assert c.invoke("hi") == "ab"
    assert c.last_tool_calls is None


def test_invoke_collects_tool_calls_from_stream():
    """stream_chat 中的 tool_call 分块被记入 last_tool_calls。"""

    class _Chat:
        def stream_chat(self, prompt, timeout=None):
            yield {"type": "tool_call", "tool_calls": [{"name": "t1"}]}

    c = LLMClient(_Chat())
    c.invoke("hi")
    assert c.last_tool_calls == [{"name": "t1"}]


def test_invoke_without_any_interface_raises():
    """底层 chat 无任何可用接口时抛 AttributeError。"""

    class _Chat:
        pass

    c = LLMClient(_Chat())
    with pytest.raises(AttributeError, match="no invoke/chat/__call__/stream_chat"):
        c.invoke("hi")


def test_chat_prefers_chat_method():
    """chat() 优先走底层 chat。"""

    class _Chat:
        def chat(self, prompt, timeout=None):
            return "chat-path"

        def invoke(self, prompt, timeout=None):
            return "invoke-path"

    c = LLMClient(_Chat())
    assert c.chat("hi") == "chat-path"


def test_chat_falls_back_to_invoke():
    """无 chat 时回落到 invoke。"""

    class _Chat:
        def invoke(self, prompt, timeout=None):
            return "invoke-path"

    c = LLMClient(_Chat())
    assert c.chat("hi") == "invoke-path"


def test_dunder_call_delegates_to_invoke():
    """__call__ 等价于 invoke。"""

    class _Chat:
        def invoke(self, prompt, timeout=None):
            return "called"

    c = LLMClient(_Chat())
    assert c("hi") == "called"


def test_retry_delay_is_fast_under_pytest():
    """pytest 环境下退避为毫秒级（避免拖慢套件）。"""
    from hero_quant.llm.client import _retry_delay

    d = _retry_delay(3)
    assert 0.0 < d < 0.2

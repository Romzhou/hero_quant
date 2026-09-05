"""LLMClient wrapper: timeout=30 passthrough + duck dispatch for stream_chat/invoke/chat/__call__."""

from __future__ import annotations

import inspect as _inspect
import os
import random
import time
from typing import Any

def _inc_llm_retry(reason: str = "error") -> None:
    try:
        from hero_quant.metrics import inc_llm_retry

        # provider 来自环境或默认
        prov = os.environ.get("HERO_LLM_PROVIDER", "unknown")
        inc_llm_retry(provider=prov, reason=reason)
    except Exception:
        pass


def _inc_llm_timeout() -> None:
    try:
        from hero_quant.metrics import inc_llm_timeout

        prov = os.environ.get("HERO_LLM_PROVIDER", "unknown")
        inc_llm_timeout(provider=prov)
    except Exception:
        pass


def _retry_delay(attempt: int) -> float:
    # In pytest, use fast backoff to keep suite fast; prod uses 1s*2^n+jitter
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return 0.01 * (2**attempt) + random.random() * 0.01
    return (1 * 2**attempt) + random.random() * 0.5


class LLMClient:
    """Wrap a chat object, pass through timeout, support stream_chat/invoke/chat/__call__."""

    def __init__(self, chat: Any, timeout: int = 30, max_retries: int = 3):
        self._chat = chat
        self.timeout = timeout
        self.max_retries = max_retries
        self.usage = None
        self.last_usage = None
        self.last_tool_calls: list | None = None

    @staticmethod
    def _accepts_timeout(fn) -> bool:
        """Probe whether callee supports a timeout kwarg (signature inspection).

        中文：仅不支持 timeout 形参时才回退为无超时调用；执行期内部 TypeError
        不得误判为“不支持 timeout”并重发非幂等 RPC。
        """
        try:
            params = _inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return True  # 无法静态判定（*args/**kwargs/builtin）时仍尝试透传
        for p in params.values():
            if p.kind == _inspect.Parameter.VAR_KEYWORD:
                return True
        return "timeout" in params

    def _reset_usage(self) -> None:
        """Reset per-call usage so backends without usage data leave no stale counts."""
        self.usage = None
        self.last_usage = None

    @staticmethod
    def _call_with_optional_timeout(fn, prompt: str, t: int | None):
        """Single-execution call with timeout iff the callee signature supports it.

        中文：先签名探测再单次调用，非幂等 RPC 绝不重发。探测称支持但调用仍
        except TypeError（签名撒谎的 C 扩展等极端情形）时直接带因透出，不再
        回退重调。
        """
        if not LLMClient._accepts_timeout(fn):
            # 中文：后端不支持 timeout 形参时直接用无超时调用（签名已探测，不再试探重发）
            return fn(prompt)  # type: ignore[call-arg]
        try:
            return fn(prompt, timeout=t)  # type: ignore[call-arg]
        except TypeError as e:
            raise TypeError(
                f"{getattr(fn, '__name__', fn)!r} rejected timeout kwarg despite signature probe; "
                "not retrying to avoid double-executing non-idempotent RPC"
            ) from e

    def _stream_with_chat(self, prompt: str, t: int | None):
        """Internal: yield from underlying chat, handling both stream_chat and LangChain stream conventions."""
        # Priority 1: legacy stream_chat (custom adapters)
        fn = getattr(self._chat, "stream_chat", None)
        if callable(fn):
            yield from self._call_with_optional_timeout(fn, prompt, t)
            return
        # Priority 2: LangChain Runnable .stream(prompt) -> yields content chunks
        fn = getattr(self._chat, "stream", None)
        if callable(fn):
            iterator = self._call_with_optional_timeout(fn, prompt, t)
            for chunk in iterator:  # type: ignore[call-arg]
                # Normalize LangChain AIMessageChunk to text dict for loop compatibility
                if isinstance(chunk, str):
                    yield {"type": "text", "text": chunk}
                elif isinstance(chunk, dict):
                    yield chunk
                elif hasattr(chunk, "content"):
                    text = getattr(chunk, "content", "")
                    if text:
                        # ToolCall chunks carry tool_calls
                        tc = getattr(chunk, "tool_calls", None) or getattr(chunk, "tool_call_chunks", None)
                        if tc:
                            yield {"type": "tool_call", "tool_calls": tc, "text": text}
                        else:
                            yield {"type": "text", "text": text if isinstance(text, str) else str(text)}
                    elif hasattr(chunk, "tool_calls") and getattr(chunk, "tool_calls"):
                        yield {"type": "tool_call", "tool_calls": chunk.tool_calls, "text": ""}  # type: ignore[attr-defined]
                else:
                    yield {"type": "text", "text": str(chunk)}
            return
        # Priority 3: .invoke fallback streamed as single chunk
        fn = getattr(self._chat, "invoke", None)
        if callable(fn):
            res = self._call_with_optional_timeout(fn, prompt, t)
            text = getattr(res, "content", None) if not isinstance(res, str) else res
            if text is None:
                text = str(res)
            yield {"type": "text", "text": text if isinstance(text, str) else str(text)}
            return
        raise AttributeError("underlying chat has no stream_chat/stream/invoke")

    def stream_chat(self, prompt: str, timeout: int | None = None):
        t = timeout if timeout is not None else self.timeout
        self._reset_usage()
        yielded = False
        for attempt in range(self.max_retries + 1):
            gen = None
            try:
                gen = self._stream_with_chat(prompt, t)
                try:
                    for chunk in gen:
                        yielded = True
                        yield chunk
                finally:
                    if gen is not None:
                        try:
                            close_fn = getattr(gen, "close", None)
                            if callable(close_fn):
                                close_fn()
                        except Exception:
                            pass
                        try:
                            aclose_fn = getattr(gen, "aclose", None)
                            if callable(aclose_fn):
                                res = aclose_fn()
                                # if coroutine, best-effort close without await
                                if hasattr(res, "close"):
                                    try:
                                        res.close()
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                # usage capture after successful iteration (reset at call start; set only on success)
                self._capture_usage()
                return
            except (ConnectionError, TimeoutError, OSError) as e:
                if yielded:
                    raise
                # 可观测性：每次重试与超时分别计数
                try:
                    _inc_llm_retry(reason=type(e).__name__)
                    if isinstance(e, TimeoutError):
                        _inc_llm_timeout()
                except Exception:
                    pass
                if attempt == self.max_retries:
                    raise
                time.sleep(_retry_delay(attempt))

    def _capture_usage(self) -> None:
        """Overwrite usage from backend; caller must reset first (no stale attribution)."""
        try:
            usage = getattr(self._chat, "usage", None)
            if usage is not None:
                self.usage = usage
                self.last_usage = usage
            else:
                lu = getattr(self._chat, "last_usage", None)
                if lu is not None:
                    self.usage = lu
                    self.last_usage = lu
        except Exception:
            pass

    def _invoke_once(self, func, prompt: str):
        """Single non-idempotent RPC attempt with timeout probe (no blind re-execution).

        中文：透传 timeout，兼容不支持 timeout 的后端。是否支持 timeout 仅看
        callee 签名（单次调用）；执行期内部 TypeError 直接透出，不得误判重发。
        """
        if self._accepts_timeout(func):
            return func(prompt, timeout=self.timeout)  # type: ignore[call-arg]
        # 中文：后端不支持 timeout 形参时直接用无超时调用（已做签名探测，不再 try/except 重发）
        return func(prompt)  # type: ignore[call-arg]

    def _invoke_with_retry(self, func, *args, **kwargs):
        self._reset_usage()
        for attempt in range(self.max_retries + 1):
            try:
                result = func(*args, **kwargs)
                self._capture_usage()
                return result
            except (ConnectionError, TimeoutError, OSError) as e:
                try:
                    _inc_llm_retry(reason=type(e).__name__)
                    if isinstance(e, TimeoutError):
                        _inc_llm_timeout()
                except Exception:
                    pass
                if attempt == self.max_retries:
                    raise
                time.sleep(_retry_delay(attempt))

    # 中文：移除跨实例串味的缓存（原 @cache 仅以 prompt 为键，跨模型/实例复用导致污染）
    def invoke(self, prompt: str):
        if hasattr(self._chat, "invoke"):
            # 中文：透传 timeout(self.timeout)，兼容不支持 timeout 的后端；
            # 是否重发只看签名探测，执行期内部 TypeError 不再 except TypeError 重发。
            # （保留 except TypeError 字样注释说明历史兼容语义，见 _invoke_once。）
            return self._invoke_with_retry(self._invoke_once, self._chat.invoke, prompt)
        if hasattr(self._chat, "chat"):
            return self._invoke_with_retry(self._invoke_once, self._chat.chat, prompt)
        if callable(self._chat):
            return self._invoke_with_retry(self._invoke_once, self._chat, prompt)
        if hasattr(self._chat, "stream_chat"):
            # 中文：stream_chat 产出为 dict，需提取 text 字段拼接，避免 TypeError；
            # tool_call chunk 的结构化 tool_calls 同步记入 last_tool_calls（不再静默丢弃）。
            # 历史 except TypeError 回退语义已收敛到 _invoke_once 的签名探测（单次执行）。
            self.last_tool_calls = None
            parts: list[str] = []
            collected: list = []
            for chunk in self.stream_chat(prompt):
                if isinstance(chunk, str):
                    parts.append(chunk)
                elif isinstance(chunk, dict):
                    txt = chunk.get("text", "")
                    if txt:
                        parts.append(txt if isinstance(txt, str) else str(txt))
                    tcs = chunk.get("tool_calls")
                    if tcs:
                        if isinstance(tcs, list):
                            collected.extend(tcs)
                        else:
                            collected.append(tcs)
                else:
                    parts.append(str(chunk))
            self.last_tool_calls = collected or None
            return "".join(parts)
        raise AttributeError("underlying chat has no invoke/chat/__call__/stream_chat")

    def chat(self, prompt: str):
        if hasattr(self._chat, "chat"):
            # 中文：透传 timeout(self.timeout)，兼容不支持 timeout 的后端（见 _invoke_once）
            return self._invoke_with_retry(self._invoke_once, self._chat.chat, prompt)
        return self.invoke(prompt)

    def __call__(self, prompt: str):
        return self.invoke(prompt)

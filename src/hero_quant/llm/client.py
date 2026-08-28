"""LLMClient wrapper: timeout=30 passthrough + duck dispatch for stream_chat/invoke/chat/__call__."""

from __future__ import annotations

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

    def stream_chat(self, prompt: str, timeout: int | None = None):
        t = timeout if timeout is not None else self.timeout
        for attempt in range(self.max_retries + 1):
            try:
                try:
                    gen = self._chat.stream_chat(prompt, timeout=t)
                except TypeError:
                    gen = self._chat.stream_chat(prompt)
                for chunk in gen:
                    yield chunk
                # usage capture after successful iteration
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
                return
            except (ConnectionError, TimeoutError, OSError) as e:
                # 可观测性：每次重试与超时分别计数
                try:
                    _inc_llm_retry(reason=type(e).__name__)
                    if isinstance(e, TimeoutError):
                        _inc_llm_timeout()
                except Exception:
                    pass
                if attempt == self.max_retries:
                    # 最终失败若为超时再计一次超时，保证计数可见
                    if isinstance(e, TimeoutError):
                        try:
                            _inc_llm_timeout()
                        except Exception:
                            pass
                    raise
                time.sleep(_retry_delay(attempt))

    def _invoke_with_retry(self, func, *args, **kwargs):
        for attempt in range(self.max_retries + 1):
            try:
                result = func(*args, **kwargs)
                try:
                    usage = getattr(self._chat, "usage", None)
                    if usage is not None:
                        self.usage = usage
                        self.last_usage = usage
                except Exception:
                    pass
                return result
            except (ConnectionError, TimeoutError, OSError) as e:
                try:
                    _inc_llm_retry(reason=type(e).__name__)
                    if isinstance(e, TimeoutError):
                        _inc_llm_timeout()
                except Exception:
                    pass
                if attempt == self.max_retries:
                    if isinstance(e, TimeoutError):
                        try:
                            _inc_llm_timeout()
                        except Exception:
                            pass
                    raise
                time.sleep(_retry_delay(attempt))

    def invoke(self, prompt: str):
        if hasattr(self._chat, "invoke"):
            return self._invoke_with_retry(self._chat.invoke, prompt)
        if hasattr(self._chat, "chat"):
            return self._invoke_with_retry(self._chat.chat, prompt)
        if callable(self._chat):
            return self._invoke_with_retry(self._chat, prompt)
        if hasattr(self._chat, "stream_chat"):
            # fallback to stream_chat as invoke
            return "".join(self.stream_chat(prompt))
        raise AttributeError("underlying chat has no invoke/chat/__call__/stream_chat")

    def chat(self, prompt: str):
        if hasattr(self._chat, "chat"):
            return self._invoke_with_retry(self._chat.chat, prompt)
        return self.invoke(prompt)

    def __call__(self, prompt: str):
        return self.invoke(prompt)

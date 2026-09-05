"""FAIL-first repro tests for Lane E3 (retest271): security + llm + otel + tools.

DO NOT edit existing test files — all repro tests live here.
Each test must FAIL before the fix and PASS after.
"""
from __future__ import annotations

import hashlib
import hmac


# ============================================================================
# api/security.py — 4 items
# ============================================================================

def test_e3_security_classic_hmac_empty_secret_fail_closed():
    """[security·high] Classic HMAC path must fail closed on empty secret/signature."""
    from hero_quant.api.security import verify_hmac

    payload = b"hello world"
    # empty secret: attacker forging with unset key must NOT verify
    forged = hmac.new(b"", payload, hashlib.sha256).hexdigest()
    assert verify_hmac(payload, forged, "") is False
    # empty signature must also fail closed
    assert verify_hmac(payload, "", "s3cr3t") is False
    # sane path still works
    sig = hmac.new(b"s3cr3t", payload, hashlib.sha256).hexdigest()
    assert verify_hmac(payload, sig, "s3cr3t") is True
    assert verify_hmac(payload, None, "s3cr3t") is False
    assert verify_hmac(payload, sig, None) is False


def test_e3_security_redis_error_falls_back_to_memory():
    """[bug·high] redis.exceptions.RedisError must be caught → memory fallback."""
    import hero_quant.api.security as sec_mod

    try:
        from redis.exceptions import ConnectionError as RedisConnectionError
    except ImportError:
        RedisConnectionError = None
    assert RedisConnectionError is not None, "redis-py required for this test"

    class BoomRedis:
        def set(self, *a, **k):
            raise RedisConnectionError("redis down")

        def getdel(self, *a, **k):
            raise RedisConnectionError("redis down")

        def eval(self, *a, **k):
            raise RedisConnectionError("redis down")

    orig = sec_mod._get_redis_for_ticket
    sec_mod._get_redis_for_ticket = lambda: BoomRedis()  # noqa: E731
    try:
        t = sec_mod.issue_ticket(ttl=60)
        assert isinstance(t, str) and t
        assert sec_mod.consume_ticket(t) is True
        assert sec_mod.consume_ticket(t) is False  # single-use
    finally:
        sec_mod._get_redis_for_ticket = orig


def test_e3_security_sync_body_explicit_empty_not_conflated():
    """[bug·medium] Explicit empty string body must NOT fall back to _body cache.

    `signature=""` falls into the request.body() else-branch (falsy str),
    then `if body == b"":` unconditionally falls back to `_body`/`_content`,
    verifying an explicit empty body against the wrong (cached) bytes.
    """
    import os

    from hero_quant.api.security import verify_hmac

    secret = "s3cr3t-e3"
    os.environ["HERO_HMAC_SECRET"] = secret
    try:
        sig_empty = hmac.new(secret.encode(), b"", hashlib.sha256).hexdigest()
        sig_cached = hmac.new(secret.encode(), b"something-else-entirely", hashlib.sha256).hexdigest()

        def _mkreq(sig):
            class CachedReq:
                headers = {"X-HMAC-Signature": sig}
                _body = b"something-else-entirely"

                def body(self):
                    async def _coro():
                        return b"something-else-entirely"

                    return _coro()

            return CachedReq()

        # explicit empty string body + sig over b"" → must verify True
        assert verify_hmac(_mkreq(sig_empty), "", secret) is True
        # no explicit body → back-compat _body fallback still works
        assert verify_hmac(_mkreq(sig_cached), None, secret) is True
    finally:
        os.environ.pop("HERO_HMAC_SECRET", None)


def test_e3_security_collision_retry_checks_second_set():
    """[bug·low] Second SET NX falsy → must NOT return un-stored ticket."""
    import hero_quant.api.security as sec_mod

    class AlwaysCollide:
        def set(self, *a, **k):
            return None  # NX collision every time

        def getdel(self, *a, **k):
            return None

    orig = sec_mod._get_redis_for_ticket
    sec_mod._get_redis_for_ticket = lambda: AlwaysCollide()  # noqa: E731
    try:
        t = sec_mod.issue_ticket(ttl=60)
        # ticket must actually be consumable (stored somewhere), not a ghost
        assert sec_mod.consume_ticket(t) is True
    finally:
        sec_mod._get_redis_for_ticket = orig


# ============================================================================
# llm/client.py — 4 items
# ============================================================================

def test_e3_llm_no_double_rpc_on_internal_typeerror():
    """[bug·high] Internal TypeError must NOT trigger timeout-fallback re-execution."""
    from hero_quant.llm.client import LLMClient

    calls = []

    class BuggyBackend:
        def invoke(self, prompt, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise TypeError("internal bug: bad prompt type")
            return "second-call-result"

    c = LLMClient(BuggyBackend(), timeout=5, max_retries=0)
    try:
        c.invoke("hi")
        raised = None
    except TypeError as e:
        raised = e
    assert raised is not None and "internal bug" in str(raised)
    assert len(calls) == 1, f"non-idempotent RPC must execute exactly once, got {len(calls)}"


def test_e3_llm_invoke_preserves_tool_calls():
    """[bug·high] invoke-via-stream_chat fallback must not silently drop tool_calls."""
    from hero_quant.llm.client import LLMClient

    class ToolBackend:
        def stream_chat(self, prompt, timeout=None):
            yield {"type": "text", "text": "thinking "}
            yield {"type": "tool_call", "tool_calls": [{"name": "get_bars", "arguments": {}}], "text": ""}

    c = LLMClient(ToolBackend(), timeout=5, max_retries=0)
    result = c.invoke("any")
    # text concatenation preserved (d3_07 compat)
    assert "thinking" in result
    # tool_calls must be retrievable, not silently discarded
    assert getattr(c, "last_tool_calls", None) == [{"name": "get_bars", "arguments": {}}]


def test_e3_llm_usage_reset_each_call():
    """[bug·medium] Usage must reset at start of each call (no stale attribution)."""
    from hero_quant.llm.client import LLMClient

    class WithUsage:
        usage = {"prompt_tokens": 10, "completion_tokens": 5}

        def stream_chat(self, p, timeout=None):
            yield "done"

    class NoUsage:
        def stream_chat(self, p, timeout=None):
            yield "done"

    c = LLMClient(WithUsage(), timeout=5, max_retries=0)
    list(c.stream_chat("first"))
    assert c.usage == {"prompt_tokens": 10, "completion_tokens": 5}
    # rebind to a backend with no usage: stale counts must NOT linger
    c._chat = NoUsage()
    list(c.stream_chat("second"))
    assert c.usage is None and c.last_usage is None


def test_e3_llm_langchain_fallback_forwards_timeout():
    """[bug·medium] stream_chat LangChain .stream/.invoke fallbacks must forward timeout."""
    from hero_quant.llm.client import LLMClient

    seen = {}

    class LangChainLike:
        def stream(self, prompt, timeout=None):
            seen["stream_t"] = timeout
            yield "chunk"

        def invoke(self, prompt, timeout=None):
            seen["invoke_t"] = timeout
            return "invoked"

    c = LLMClient(LangChainLike(), timeout=9, max_retries=0)
    chunks = list(c.stream_chat("hi"))
    assert chunks and seen.get("stream_t") == 9, f"stream fallback must get timeout=9, got {seen}"

    class InvokeOnly:
        def invoke(self, prompt, timeout=None):
            seen["direct_t"] = timeout
            return "ok"

    c2 = LLMClient(InvokeOnly(), timeout=11, max_retries=0)
    assert c2.invoke("hi") == "ok"
    assert seen.get("direct_t") == 11, f"invoke must get timeout=11, got {seen}"

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


# ============================================================================
# telemetry/otel.py — 2 items
# ============================================================================

def test_e3_otel_dns_no_global_socket_mutation():
    """[bug·high] _cached_getaddrinfo must not touch socket.setdefaulttimeout (process-global)."""
    import socket

    import hero_quant.telemetry.otel as otel_mod

    otel_mod._clear_dns_cache()
    calls = {"n": 0}
    orig_resolver = socket.getaddrinfo

    def counting(host, *a, **k):
        calls["n"] += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]

    seen_defaults = []
    orig_setdefault = socket.setdefaulttimeout

    def spy_setdefault(t):
        seen_defaults.append(t)
        return orig_setdefault(t)

    socket.getaddrinfo = counting  # noqa
    socket.setdefaulttimeout = spy_setdefault  # noqa
    try:
        before = socket.getdefaulttimeout()
        otel_mod._cached_getaddrinfo("e3-no-mutate.invalid")
        otel_mod._cached_getaddrinfo("e3-no-mutate.invalid")
        after = socket.getdefaulttimeout()
        assert seen_defaults == [], f"must not touch global default, saw {seen_defaults}"
        assert after == before, "pre-existing default must be preserved"
        assert calls["n"] <= 1, "TTL cache must still work"
    finally:
        socket.getaddrinfo = orig_resolver  # noqa
        socket.setdefaulttimeout = orig_setdefault  # noqa
        otel_mod._clear_dns_cache()


def test_e3_otel_provider_build_race_no_leak():
    """[bug·medium] Concurrent export() must not leak the loser's provider/processor."""
    import threading

    import hero_quant.telemetry.otel as otel_mod
    from hero_quant.telemetry.otel import SessionTelemetryCoordinator

    created = []
    shut = {"n": 0}
    entered = threading.Event()
    release = threading.Event()

    class FakeProcessor:
        def shutdown(self):
            shut["n"] += 1

    class FakeProvider:
        def __init__(self):
            self.processor = None

        def add_log_record_processor(self, p):
            self.processor = p

        def get_logger(self, name):
            return None

        def force_flush(self, timeout_millis=None):
            return True

        def shutdown(self):
            shut["n"] += 1

    class FakeExporter:
        def __init__(self, *a, **k):
            created.append(1)
            if len(created) == 1:
                entered.set()
                assert release.wait(timeout=10), "second builder never arrived"

    import sys
    import types

    monkey_mods = {}
    try:
        import opentelemetry.sdk._logs as sdk_logs  # noqa
        import opentelemetry.sdk._logs.export as sdk_export  # noqa
        real_lp, real_blrp = sdk_logs.LoggerProvider, sdk_export.BatchLogRecordProcessor
        sdk_logs.LoggerProvider = FakeProvider  # noqa
        sdk_export.BatchLogRecordProcessor = lambda e: FakeProcessor()  # noqa
        monkey_mods["sdk"] = (sdk_logs, sdk_export, real_lp, real_blrp)
    except ImportError:
        sdk_pkg = types.ModuleType("opentelemetry.sdk")
        logs_mod = types.ModuleType("opentelemetry.sdk._logs")
        logs_mod.LoggerProvider = FakeProvider
        export_mod = types.ModuleType("opentelemetry.sdk._logs.export")
        export_mod.BatchLogRecordProcessor = lambda e: FakeProcessor()
        sys.modules["opentelemetry.sdk"] = sdk_pkg
        sys.modules["opentelemetry.sdk._logs"] = logs_mod
        sys.modules["opentelemetry.sdk._logs.export"] = export_mod
        monkey_mods["sys"] = True

    try:
        import opentelemetry.exporter.otlp.proto.http._log_exporter as http_mod  # noqa
        real_exp = http_mod.OTLPLogExporter
        http_mod.OTLPLogExporter = FakeExporter  # noqa
        monkey_mods["http"] = (http_mod, real_exp)
    except ImportError:
        for pkg in ("opentelemetry", "opentelemetry.exporter", "opentelemetry.exporter.otlp",
                    "opentelemetry.exporter.otlp.proto", "opentelemetry.exporter.otlp.proto.http"):
            sys.modules.setdefault(pkg, types.ModuleType(pkg))
        http_mod = types.ModuleType("opentelemetry.exporter.otlp.proto.http._log_exporter")
        http_mod.OTLPLogExporter = FakeExporter
        sys.modules["opentelemetry.exporter.otlp.proto.http._log_exporter"] = http_mod
        monkey_mods.setdefault("sys", True)

    import os
    import socket

    old_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://example.com/v1/logs"
    real_gai = socket.getaddrinfo
    socket.getaddrinfo = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]  # noqa
    otel_mod._OTEL_CACHED_PROVIDER = None
    otel_mod._OTEL_CACHED_PROCESSOR = None
    otel_mod._OTEL_CACHED_ENDPOINT = None
    coord = SessionTelemetryCoordinator(mode="private")
    errs = []

    def run_export():
        try:
            coord.export({"e": 1})
        except Exception as e:  # noqa
            errs.append(e)

    try:
        t1 = threading.Thread(target=run_export)
        t2 = threading.Thread(target=run_export)
        t1.start()
        assert entered.wait(timeout=10), "first builder never started"
        t2.start()
        release.set()
        t1.join(timeout=20)
        t2.join(timeout=20)
        assert not errs, f"export raised: {errs}"
        # two providers built, exactly one published → loser must be shut down
        assert len(created) == 2, f"expected 2 concurrent builds, got {len(created)}"
        assert shut["n"] >= 1, "loser provider/processor must be shut down, not leaked"
        assert otel_mod._OTEL_CACHED_PROVIDER is not None
    finally:
        release.set()
        socket.getaddrinfo = real_gai  # noqa
        if old_endpoint is None:
            os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        else:
            os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = old_endpoint
        if "sdk" in monkey_mods:
            sdk_logs, sdk_export, real_lp, real_blrp = monkey_mods["sdk"]
            sdk_logs.LoggerProvider = real_lp  # noqa
            sdk_export.BatchLogRecordProcessor = real_blrp  # noqa
        if "http" in monkey_mods:
            http_mod, real_exp = monkey_mods["http"]
            http_mod.OTLPLogExporter = real_exp  # noqa
        if monkey_mods.get("sys") and "sdk" not in monkey_mods:
            for m in ("opentelemetry.sdk._logs.export", "opentelemetry.sdk._logs", "opentelemetry.sdk",
                      "opentelemetry.exporter.otlp.proto.http._log_exporter"):
                sys.modules.pop(m, None)
        otel_mod.shutdown_otel()


# ============================================================================
# tools/registry.py — 4 items
# ============================================================================

def test_e3_registry_truthy_concurrency_rejected():
    """[bug·high] Non-bool/non-callable is_concurrency_safe must fail fast, not coerce truthy."""
    import pytest

    from hero_quant.tools.registry import TOOL_REGISTRY, tool

    with pytest.raises(ValueError):
        @tool(name="e3_bad_safe_xyz", description="bad safe", is_concurrency_safe="false")
        def _f():
            pass

    assert "e3_bad_safe_xyz" not in TOOL_REGISTRY
    # bool/callable still fine
    @tool(name="e3_good_safe_xyz", description="good safe", is_concurrency_safe=True)
    def _g():
        pass

    assert TOOL_REGISTRY.pop("e3_good_safe_xyz").is_concurrency_safe({}) is True


def test_e3_registry_timeoutms_strict():
    """[bug·medium] timeoutMs must reject bool and non-integral floats (no silent coercion)."""
    import pytest

    from hero_quant.tools.registry import TOOL_REGISTRY, tool

    with pytest.raises(ValueError):
        @tool(name="e3_bool_timeout_xyz", description="bad", timeoutMs=True)
        def _f():
            pass

    with pytest.raises(ValueError):
        @tool(name="e3_float_timeout_xyz", description="bad", timeoutMs=1.9)
        def _g():
            pass

    # integral float and int-convertible str still accepted
    @tool(name="e3_ok_timeout_xyz", description="ok", timeoutMs=5.0)
    def _h():
        pass

    assert TOOL_REGISTRY.pop("e3_ok_timeout_xyz").timeoutMs == 5
    assert "e3_bool_timeout_xyz" not in TOOL_REGISTRY
    assert "e3_float_timeout_xyz" not in TOOL_REGISTRY


def test_e3_registry_explicit_plus_alias_conflict():
    """[bug·medium] Explicit timeoutMs + timeout_ms alias must raise conflicting-alias error."""
    import pytest

    from hero_quant.tools.registry import TOOL_REGISTRY, tool

    with pytest.raises(ValueError, match="conflicting timeout aliases"):
        @tool(name="e3_conflict_xyz", description="bad", timeoutMs=100, timeout_ms=200)
        def _f():
            pass

    assert "e3_conflict_xyz" not in TOOL_REGISTRY


def test_e3_registry_wrapped_output_exact_shape():
    """[bug·medium] Only exact {schema, render} counts as wrapped; validated at use time."""
    import pytest

    from hero_quant.tools.registry import TOOL_REGISTRY, tool

    raw_schema_with_those_keys = {
        "type": "object",
        "properties": {
            "schema": {"type": "string"},
            "render": {"type": "string"},
        },
    }
    # raw schema that happens to use schema/render keys must be treated as a
    # whole raw schema (wrapped once more), NOT unwrapped as {"schema", "render"} form
    @tool(name="e3_raw_schema_xyz", description="raw", output=raw_schema_with_those_keys)
    def _f():
        pass

    stored = TOOL_REGISTRY.pop("e3_raw_schema_xyz").output
    assert stored == {"schema": raw_schema_with_those_keys, "render": None}, stored

    # exact wrapped shape still supported and kept as-is
    @tool(
        name="e3_wrapped_xyz",
        description="wrapped",
        output={"schema": {"type": "object", "properties": {}}, "render": None},
    )
    def _g():
        pass

    stored2 = TOOL_REGISTRY.pop("e3_wrapped_xyz").output
    assert stored2 == {"schema": {"type": "object", "properties": {}}, "render": None}

    # TOCTOU: mutating the dict between tool(...) call and decorator application
    # must not bypass fail-fast validation
    evil = {"type": "object", "properties": {}}
    factory = tool(name="e3_toctou_xyz", description="toctou", output=evil)
    evil.clear()
    evil["type"] = "not-a-real-type"
    with pytest.raises(ValueError):
        @factory
        def _h():
            pass

    assert "e3_toctou_xyz" not in TOOL_REGISTRY


# ============================================================================
# tools/redaction.py — 2 items
# ============================================================================

def test_e3_redaction_set_json_not_repr():
    """[bug·medium] set/frozenset results must serialize as JSON arrays, not Python repr."""
    import json

    from hero_quant.tools.redaction import redact_tool_result

    s = redact_tool_result({3, 1, 2}, sink="result")
    assert s == "[1, 2, 3]", s
    parsed = json.loads(s)
    assert sorted(parsed) == [1, 2, 3]

    s2 = redact_tool_result(frozenset({"b", "a"}), sink="result")
    assert json.loads(s2) == ["a", "b"], s2


def test_e3_redaction_fail_closed_sentinel_unquoted():
    """[bug·medium] Fail-closed *** sentinel must stay unquoted on the dict/list path."""
    from hero_quant.tools import redaction as red_mod

    orig = red_mod._maybe_redact
    red_mod._maybe_redact = lambda value, sink="result": "***"  # noqa: E731
    try:
        from hero_quant.tools.redaction import redact_tool_result

        assert redact_tool_result({"k": "v"}, sink="result") == "***"
        assert redact_tool_result(["a"], sink="result") == "***"
    finally:
        red_mod._maybe_redact = orig


# ============================================================================
# tools/presentation.py — 1 item
# ============================================================================

def test_e3_presentation_multiline_description_commented():
    """[bug·low] Every description line must be #-prefixed in code rendering."""
    from hero_quant.tools.presentation import present_as_code

    out = present_as_code({"name": "demo", "description": "line one\nline two\n\nline four"})
    lines = out.splitlines()
    assert lines[0] == "# Tool: demo"
    for ln in lines[1:]:
        assert ln.startswith("#"), f"uncommented overflow line: {ln!r}"
    assert "line two" in out and "line four" in out


def test_e3_llm_stream_no_timeout_backend_single_execution():
    """[bug·high] stream_chat to a no-timeout backend must execute the RPC exactly once."""
    from hero_quant.llm.client import LLMClient

    calls = []

    class NoTimeoutStream:
        def stream_chat(self, prompt):
            calls.append(1)
            yield "ok"

    c = LLMClient(NoTimeoutStream(), timeout=7, max_retries=0)
    chunks = list(c.stream_chat("hi"))
    assert chunks == ["ok"]
    assert len(calls) == 1, f"non-idempotent stream RPC must execute once, got {len(calls)}"


def test_e3_registry_parameters_mutation_window_closed():
    """[bug·high] Mutating parameters between tool(...) and decorator must not bypass validation."""
    import pytest

    from hero_quant.tools.registry import TOOL_REGISTRY, tool

    params = {"type": "object", "properties": {}}
    factory = tool(name="e3_params_toctou_xyz", description="toctou", parameters=params)
    params.clear()
    params["type"] = "not-a-real-type"
    with pytest.raises(ValueError):
        @factory
        def _h():
            pass

    assert "e3_params_toctou_xyz" not in TOOL_REGISTRY

"""Provider 网关契约：顺序回退、降级可见、流式只在首 chunk 前回退、密钥不进日志。"""

from __future__ import annotations

import pytest

from hero_quant.llm.gateway import AllProvidersFailed, LLMGateway, ProviderSpec
from hero_quant.telemetry.circuit import CircuitBreaker


class StubClient:
    """可控的假 provider 客户端：记录调用次数，按需抛错/返回空文本。"""

    def __init__(self, text: str = "ok", exc: BaseException | None = None, chunks: list | None = None):
        self.text = text
        self.exc = exc
        self.chunks = chunks
        self.chat_calls = 0
        self.stream_calls = 0
        self.last_usage = None
        self.usage = None

    def chat(self, prompt, timeout=None):
        self.chat_calls += 1
        if self.exc is not None:
            raise self.exc
        return self.text

    def stream_chat(self, prompt, timeout=None):
        self.stream_calls += 1
        if self.exc is not None:
            raise self.exc
        if self.chunks is not None:
            yield from self.chunks
            return
        yield {"type": "text", "text": self.text}


def _breaker() -> CircuitBreaker:
    return CircuitBreaker(failure_threshold=0.5, window=60, open_duration=60)


def _gateway(
    clients: dict[str, StubClient],
    *,
    trip_after_failures: int = 2,
) -> LLMGateway:
    specs = [ProviderSpec(name=name, model="gpt-4o-mini") for name in clients]
    return LLMGateway(
        specs,
        client_factory=lambda spec: clients[spec.name],
        breaker_factory=_breaker,
        trip_after_failures=trip_after_failures,
    )


def test_primary_success_is_not_degraded():
    clients = {"openai": StubClient("primary-ok"), "deepseek": StubClient("secondary")}
    gw = _gateway(clients)

    resp = gw.complete("hi")

    assert resp.text == "primary-ok"
    assert resp.provider == "openai"
    assert resp.degraded is False
    assert gw.degraded is False
    assert gw.degraded_reason is None
    assert clients["deepseek"].chat_calls == 0


def test_failover_to_secondary_is_marked_degraded():
    clients = {
        "openai": StubClient(exc=ConnectionError("primary down")),
        "deepseek": StubClient("secondary-ok"),
    }
    gw = _gateway(clients)

    resp = gw.complete("hi")

    assert resp.text == "secondary-ok"
    assert resp.provider == "deepseek"
    assert resp.degraded is True
    assert resp.attempts == 2
    assert gw.degraded is True
    assert "provider_failover:openai->deepseek" in (gw.degraded_reason or "")
    snap = {p["provider"]: p for p in gw.health()["providers"]}
    assert snap["openai"]["failures"] == 1
    assert snap["openai"]["last_error"] == "ConnectionError"


def test_empty_response_counts_as_failure_and_fails_over():
    clients = {"openai": StubClient("   "), "deepseek": StubClient("real text")}
    gw = _gateway(clients)

    resp = gw.complete("hi")

    assert resp.text == "real text"
    assert resp.degraded is True
    assert resp.provider == "deepseek"


def test_all_providers_failed_raises_and_never_fakes_output():
    clients = {
        "openai": StubClient(exc=ConnectionError("down")),
        "deepseek": StubClient(exc=TimeoutError("slow")),
    }
    gw = _gateway(clients)

    with pytest.raises(AllProvidersFailed) as excinfo:
        gw.chat("hi")

    attempts = excinfo.value.attempts
    assert [a["provider"] for a in attempts] == ["openai", "deepseek"]
    assert [a["error"] for a in attempts] == ["ConnectionError", "TimeoutError"]
    assert gw.degraded is True
    assert str(gw.degraded_reason or "").startswith("all_providers_failed")


def test_error_message_never_leaks_secret():
    class AuthError(Exception):
        pass

    clients = {
        "openai": StubClient(exc=AuthError("Incorrect API key sk-supersecret123")),
        "deepseek": StubClient(exc=AuthError("bad key sk-supersecret123")),
    }
    gw = _gateway(clients)

    with pytest.raises(AllProvidersFailed) as excinfo:
        gw.chat("hi")

    assert "sk-supersecret123" not in str(excinfo.value)
    blob = str(gw.health())
    assert "sk-supersecret123" not in blob


def test_provider_spec_and_health_never_expose_key():
    spec = ProviderSpec(name="openai", model="gpt-4o-mini", api_key="sk-supersecret123")

    assert "sk-supersecret123" not in repr(spec)
    assert "sk-supersecret123" not in str(spec.safe_dict())
    assert spec.safe_dict()["has_key"] is True


def test_breaker_skips_provider_after_consecutive_failures():
    clients = {
        "openai": StubClient(exc=ConnectionError("down")),
        "deepseek": StubClient("secondary-ok"),
    }
    gw = _gateway(clients, trip_after_failures=2)

    assert gw.chat("1") == "secondary-ok"
    assert gw.chat("2") == "secondary-ok"
    # 第 3 次：openai 已在熔断窗口内 OPEN，应直接跳过（不再 true 调用）
    assert gw.chat("3") == "secondary-ok"

    assert clients["openai"].chat_calls == 2
    snap = {p["provider"]: p for p in gw.health()["providers"]}
    assert snap["openai"]["state"] == "OPEN"
    assert snap["openai"]["attempts"] == 2


def test_stream_failover_before_first_chunk():
    clients = {
        "openai": StubClient(exc=ConnectionError("down")),
        "deepseek": StubClient(chunks=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]),
    }
    gw = _gateway(clients)

    chunks = list(gw.stream_chat("hi"))

    assert [c["text"] for c in chunks] == ["a", "b"]
    assert gw.active_provider == "deepseek"
    assert gw.degraded is True


def test_stream_does_not_failover_after_partial_output():
    class PartialThenFail(StubClient):
        def stream_chat(self, prompt, timeout=None):
            self.stream_calls += 1
            yield {"type": "text", "text": "first"}
            raise ConnectionError("mid-stream")

    clients = {"openai": PartialThenFail(), "deepseek": StubClient("secondary")}
    gw = _gateway(clients)

    received = []
    with pytest.raises(ConnectionError):
        for chunk in gw.stream_chat("hi"):
            received.append(chunk["text"])

    assert received == ["first"]
    assert clients["deepseek"].stream_calls == 0, "已产出内容后不得重发（会重复执行非幂等 RPC）"


def test_stream_empty_is_failure():
    clients = {"openai": StubClient(chunks=[]), "deepseek": StubClient("fallback")}
    gw = _gateway(clients)

    chunks = list(gw.stream_chat("hi"))

    assert [c["text"] for c in chunks] == ["fallback"]
    assert gw.degraded is True


def test_health_check_reports_degraded_when_primary_down():
    clients = {
        "openai": StubClient(exc=ConnectionError("down")),
        "deepseek": StubClient("pong"),
    }
    gw = _gateway(clients)

    health = gw.health_check()

    assert health["ok"] is True
    assert health["degraded"] is True
    assert health["active"] == "deepseek"
    by_name = {p["provider"]: p for p in health["providers"]}
    assert by_name["openai"]["ok"] is False
    assert by_name["openai"]["error_code"] == "ConnectionError"
    assert by_name["deepseek"]["ok"] is True


def test_health_check_all_down_is_not_ok():
    clients = {
        "openai": StubClient(exc=ConnectionError("down")),
        "deepseek": StubClient(exc=TimeoutError("slow")),
    }
    gw = _gateway(clients)

    health = gw.health_check()

    assert health["ok"] is False
    assert health["degraded"] is False  # 全挂不是 degraded，是 unavailable
    assert health["degraded_reason"] == "all_providers_failed:health_check"


def test_from_settings_without_provider_keys_keeps_single_chain(monkeypatch):
    class FakeSettings:
        llm_provider = "openai"
        llm_model = "gpt-4o-mini"
        api_key = None
        openai_api_key = None

    for key in ("OPENAI_API_KEY", "HERO_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
        monkeypatch.setenv(key, "")

    gw = LLMGateway.from_settings(FakeSettings())

    assert gw.chain == ["openai"]


def test_from_settings_adds_fallback_only_when_provider_key_present(monkeypatch):
    class FakeSettings:
        llm_provider = "openai"
        llm_model = "gpt-4o-mini"
        api_key = "sk-primary"

    for key in ("OPENAI_API_KEY", "HERO_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")

    gw = LLMGateway.from_settings(FakeSettings())

    assert gw.chain == ["openai", "deepseek"]
    assert "sk-deepseek" not in str(gw.health())


def test_missing_key_maps_to_stable_error_code(monkeypatch):
    class FakeSettings:
        llm_provider = "openai"
        llm_model = "gpt-4o-mini"
        api_key = None
        openai_api_key = None

    for key in ("OPENAI_API_KEY", "HERO_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
        monkeypatch.setenv(key, "")

    gw = LLMGateway.from_settings(FakeSettings())
    health = gw.health_check()

    assert health["ok"] is False
    assert health["providers"][0]["error_code"] == "api_key_missing"


def test_duplicate_provider_names_rejected():
    with pytest.raises(ValueError):
        LLMGateway([ProviderSpec(name="openai"), ProviderSpec(name="openai")])


def test_unknown_provider_rejected():
    with pytest.raises(ValueError):
        ProviderSpec(name="acme-llm")

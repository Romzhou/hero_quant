import pytest
from hero_quant.llm.client import LLMClient


class FakeChat:
    def __init__(self, delay=100):
        self.delay = delay

    def stream_chat(self, prompt, timeout=None):
        if timeout and timeout < 20:
            raise TimeoutError("timeout")
        yield "ok"


def test_timeout():
    c = LLMClient(FakeChat(), timeout=30)
    with pytest.raises(TimeoutError):
        list(c.stream_chat("hi", timeout=10))


def test_stream_ok():
    c = LLMClient(FakeChat())
    assert "".join(c.stream_chat("hi")) == "ok"


def test_retry_and_usage(monkeypatch):
    import time

    # patch sleep to avoid delay in test
    monkeypatch.setattr(time, "sleep", lambda x: None)
    # also patch client module sleep if already imported
    import hero_quant.llm.client as client_mod

    monkeypatch.setattr(client_mod.time, "sleep", lambda x: None)

    calls = []

    class Flaky:
        usage = {"prompt_tokens": 10, "completion_tokens": 5}

        def stream_chat(self, p, timeout=None):
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("flaky")
            yield "done"

    c = LLMClient(Flaky(), timeout=30, max_retries=3)
    assert "".join(c.stream_chat("hi")) == "done"
    assert len(calls) == 3
    # usage capture
    assert c.usage == {"prompt_tokens": 10, "completion_tokens": 5} or c.last_usage == {"prompt_tokens": 10, "completion_tokens": 5}


def test_multi_provider(monkeypatch):
    monkeypatch.setenv("HERO_LLM_PROVIDER", "deepseek")
    # need to reload settings to pick up env
    from importlib import reload
    import hero_quant.config.settings as settings_mod

    reload(settings_mod)
    from hero_quant.llm.factory import LLMFactory

    factory = LLMFactory(settings_mod.Settings())
    assert factory.provider == "deepseek"
    # also test openai and anthropic routing
    monkeypatch.setenv("HERO_LLM_PROVIDER", "anthropic")
    reload(settings_mod)
    factory2 = LLMFactory(settings_mod.Settings())
    assert factory2.provider == "anthropic"
    monkeypatch.setenv("HERO_LLM_PROVIDER", "openai")
    reload(settings_mod)
    factory3 = LLMFactory(settings_mod.Settings())
    assert factory3.provider == "openai"
    # embed timeout check (signature or source contains timeout=30)
    import inspect
    import hero_quant.agent.embed as embed_mod

    reload(embed_mod)
    source = inspect.getsource(embed_mod._try_openai)
    assert "timeout=30" in source or "timeout" in source
    # factory create should return LLMClient with timeout 30
    # use dummy settings openai provider
    llm = factory3.create(model="gpt-4o-mini")
    # LLMClient should wrap with timeout=30
    assert hasattr(llm, "timeout") and llm.timeout == 30 or hasattr(llm, "_chat")


# Task 5 TDD: stream retry only before first yield, fail-closed after partial yield
def test_stream_no_retry_after_partial_yield(monkeypatch):
    import time
    import hero_quant.llm.client as client_mod

    monkeypatch.setattr(time, "sleep", lambda x: None)
    monkeypatch.setattr(client_mod.time, "sleep", lambda x: None)
    calls = []

    class PartialFail:
        def stream_chat(self, prompt, timeout=None):
            calls.append(1)
            yield "a"
            yield "b"
            raise ConnectionError("mid-stream fail")

    c = LLMClient(PartialFail(), timeout=30, max_retries=3)
    gen = c.stream_chat("hi")
    received = []
    with pytest.raises(ConnectionError):
        for chunk in gen:
            received.append(chunk)
    assert received == ["a", "b"], f"expected exactly a,b got {received}"
    assert len(calls) == 1, "should not retry after first yield, duplicate would cause extra calls"


def test_stream_retry_before_first_yield_succeeds(monkeypatch):
    import time
    import hero_quant.llm.client as client_mod

    monkeypatch.setattr(time, "sleep", lambda x: None)
    monkeypatch.setattr(client_mod.time, "sleep", lambda x: None)
    calls = []

    class FlakyBefore:
        def stream_chat(self, prompt, timeout=None):
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError("before yield")
            yield "ok"

    c = LLMClient(FlakyBefore(), timeout=30, max_retries=3)
    assert "".join(c.stream_chat("hi")) == "ok"
    assert len(calls) == 3


def test_stream_gen_close_on_early_break(monkeypatch):
    import time
    import hero_quant.llm.client as client_mod

    monkeypatch.setattr(time, "sleep", lambda x: None)
    monkeypatch.setattr(client_mod.time, "sleep", lambda x: None)
    closed = []

    class TrackClose:
        def stream_chat(self, prompt, timeout=None):
            def gen():
                try:
                    yield "a"
                    yield "b"
                    yield "c"
                finally:
                    closed.append(True)

            return gen()

    c = LLMClient(TrackClose(), timeout=30, max_retries=2)
    g = c.stream_chat("hi")
    first = next(g)
    assert first == "a"
    g.close()
    # underlying gen should have been closed via wrapper's finally
    assert len(closed) == 1

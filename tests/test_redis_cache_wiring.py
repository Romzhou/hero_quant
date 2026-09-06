"""PR1-C: redis cache wiring for market/llm/memory (fakeredis, no real Redis)."""

import importlib
import json
import unittest.mock as mock

import fakeredis
import pytest


@pytest.fixture()
def _redis():
    import hero_quant.infra.redis as rmod

    rmod.clear_redis_instance()
    fake = fakeredis.FakeRedis(decode_responses=True)
    rmod.set_redis_instance(fake)
    yield fake
    try:
        fake.flushall()
    except Exception:
        pass
    rmod.clear_redis_instance()


def _live_settings(monkeypatch):
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import hero_quant.config.settings as s

    importlib.reload(s)
    try:
        from hero_quant.data.registry import clear_settings_cache

        clear_settings_cache()
    except Exception:
        pass
    return s


def _fake_tencent_response():
    data = {"data": {"sh600519": {"day": [["2026-09-01", 10, 11, 12, 9, 100], ["2026-09-02", 11, 12, 13, 10, 110]]}}}
    text = json.dumps(data).encode()
    m = mock.MagicMock()
    m.read.return_value = text
    m.__enter__ = lambda self: self
    m.__exit__ = lambda self, *args: False
    return m


def test_market_bars_cached_second_hit(monkeypatch, _redis):
    _live_settings(monkeypatch)
    from hero_quant.data.loaders.tencent import TencentLoader

    calls = {"n": 0}

    def fake_urlopen(url, timeout=2):
        calls["n"] += 1
        return _fake_tencent_response()

    loader = TencentLoader()
    with mock.patch("hero_quant.data.loaders.tencent.time.sleep"):
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            r1 = loader.get_bars("600519.SH", "2026-09-01", "2026-09-03")
            r2 = loader.get_bars("600519.SH", "2026-09-01", "2026-09-03")
    assert r1 == r2 and len(r1) == 2
    assert calls["n"] == 1, f"second call must hit cache, https calls={calls['n']}"
    keys = [k for k in _redis.keys("*") if str(k).startswith("hero:cache:market:")]
    assert keys, "expected hero:cache:market:* entry"


def test_llm_invoke_no_cross_instance_cache(_redis):
    """LLM invoke 不再缓存：原 @cache 仅以 prompt 为键，跨模型/实例复用导致污染，已移除。"""
    from hero_quant.llm.client import LLMClient

    calls = {"n": 0}

    class OnlyStream:
        def stream_chat(self, prompt, timeout=None):
            calls["n"] += 1
            yield "hello "
            yield "world"

    c = LLMClient(OnlyStream(), timeout=30, max_retries=1)
    r1 = c.invoke("same prompt")
    r2 = c.invoke("same prompt")
    assert r1 == r2 == "hello world"
    # 中文：缓存已移除，每次 invoke 都重新调用（避免跨实例串味）
    assert calls["n"] == 2, f"invoke 应不缓存（缓存已移除避免串味），stream calls={calls['n']}"


def test_memory_search_cached_second_hit(tmp_path, _redis):
    from hero_quant.memory.store import MemoryStore

    st = MemoryStore(tmp_path)
    try:
        st.write("k1", "hero quant cache wiring alpha beta gamma")
        st.write("k2", "unrelated zeta omega")
        orig = st.vector_search
        calls = {"n": 0}

        def counting(query, top_k=5):
            calls["n"] += 1
            return orig(query, top_k=top_k)

        st.vector_search = counting  # type: ignore[method-assign]
        r1 = st.search("cache wiring alpha")
        assert r1, "expected search hits"
        st.clear_retrieval_cache()  # drop L1 only, L2 redis must still serve
        assert st._retrieval_cache == {} and st._vector_cache == {}
        r2 = st.search("cache wiring alpha")
        assert [d["content"] for d in r2] == [d["content"] for d in r1]
        assert calls["n"] == 1, f"second search must hit L2, vector calls={calls['n']}"
        keys = [k for k in _redis.keys("*") if str(k).startswith("hero:cache:memory:search:")]
        assert keys, "expected hero:cache:memory:search:* entry"
    finally:
        st.close()

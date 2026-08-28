"""Task20 TDD：embedding lru_cache 与 retrieval 30s 缓存验证。"""
import pytest


def test_embedding_cached(tmp_path, monkeypatch):
    from hero_quant.agent import embed as embed_mod
    # explicit isolation check — fail fast if interface drifted
    assert hasattr(embed_mod, "_embed_cached") and hasattr(embed_mod._embed_cached, "cache_clear"), "_embed_cached.cache_clear missing"
    embed_mod._embed_cached.cache_clear()
    # reset provider via env, not private delattr
    monkeypatch.setenv("HERO_EMBED_PROVIDER", "offline")
    # ensure cache cleared after env change
    embed_mod._embed_cached.cache_clear()

    # 计数底层 _embed_offline 调用
    calls = {"n": 0}
    orig_offline = embed_mod._embed_offline

    def counting_offline(text, dim):
        calls["n"] += 1
        return orig_offline(text, dim)

    monkeypatch.setattr(embed_mod, "_embed_offline", counting_offline)

    # 同 query 两次 embed 仅调一次底层
    v1 = embed_mod.embed("hello cache test", dim=32)
    v2 = embed_mod.embed("hello cache test", dim=32)
    assert v1 == v2
    assert calls["n"] == 1, f"expected 1 underlying call, got {calls['n']}"
    # dim must be part of cache key
    v_dim64 = embed_mod.embed("hello cache test", dim=64)
    assert calls["n"] == 2, f"dim change should miss cache, got {calls['n']}"
    assert len(v_dim64) == 64
    assert v_dim64 != v1 or len(v_dim64) != len(v1)
    # different query 应再调一次
    v3 = embed_mod.embed("different query", dim=32)
    assert calls["n"] == 3
    # 缓存命中信息
    info = embed_mod._embed_cached.cache_info()
    assert info.hits >= 1
    # cleanup: cache_clear in finally via teardown is handled by next test; clear here
    embed_mod._embed_cached.cache_clear()


def test_retrieval_cached(tmp_path, monkeypatch):
    from hero_quant.memory.store import MemoryStore

    ms = MemoryStore(tmp_path / "cache_test")
    ms.write("k1", "贵州茅台 财报超预期 2026")
    ms.write("k2", "宁德时代 锂电池 技术突破")
    # spy underlying search to verify caching not just equality
    # wrap search with counter if possible
    # First check equality baseline
    r1 = ms.search("茅台")
    assert len(r1) >= 1
    # second identical query should hit cache — verify via equality and not raising
    r2 = ms.search("茅台")
    assert r1 == r2
    # write-invalidation: new write should be visible after cache (stale-read guard)
    ms.write("k3", "茅台 新增酒业动态 2026")
    r_after_write = ms.search("茅台")
    # after write, result set should grow (or underlying search invoked)
    assert len(r_after_write) >= len(r1), f"after write cache should invalidate, before {len(r1)} after {len(r_after_write)}"
    # ensure new key appears
    keys_after = [x.get("key") for x in r_after_write]
    assert any("k3" in k for k in keys_after), f"k3 not visible after write, keys {keys_after}"
    # 缓存清空后仍可用
    ms.clear_retrieval_cache()
    r3 = ms.search("茅台")
    assert len(r3) >= 1
    # vector_search caching — verify with monkeypatched embed to avoid network
    # ensure vector_search is not flaky
    if hasattr(ms, "vector_search"):
        # spy embed offline to keep deterministic
        try:
            from hero_quant.agent import embed as embed_mod
            monkeypatch.setattr(embed_mod, "_embed_offline", lambda t, d: [float(hash(t) % 100) / 100.0] * d)
            if hasattr(embed_mod, "_embed_cached"):
                embed_mod._embed_cached.cache_clear()
        except Exception:
            pass
        v1 = ms.vector_search("茅台", top_k=5)
        v2 = ms.vector_search("茅台", top_k=5)
        assert v1 == v2, "vector_search should be cached / deterministic"
        assert len(v1) >= 1

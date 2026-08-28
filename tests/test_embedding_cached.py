"""Task20 TDD：embedding lru_cache 与 retrieval 30s 缓存验证。"""

def test_embedding_cached(tmp_path, monkeypatch):
    from hero_quant.agent import embed as embed_mod
    # 清空缓存以保证隔离
    try:
        embed_mod._embed_cached.cache_clear()
    except Exception:
        pass
    # 计数底层 _embed_offline 调用
    calls = {"n": 0}
    orig_offline = embed_mod._embed_offline

    def counting_offline(text, dim):
        calls["n"] += 1
        return orig_offline(text, dim)

    monkeypatch.setattr(embed_mod, "_embed_offline", counting_offline)
    # 也计数语义桩，但本次 provider=offline 仅走 offline
    monkeypatch.setenv("HERO_EMBED_PROVIDER", "offline")
    # 重新计算 provider 缓存需清理
    try:
        embed_mod._embed_cached.cache_clear()
        if hasattr(embed_mod._embed_cached, "_last_provider"):
            delattr(embed_mod._embed_cached, "_last_provider")
    except Exception:
        pass

    # 同 query 两次 embed 仅调一次底层
    v1 = embed_mod.embed("hello cache test", dim=32)
    v2 = embed_mod.embed("hello cache test", dim=32)
    assert v1 == v2
    assert calls["n"] == 1, f"expected 1 underlying call, got {calls['n']}"
    # 不同 query 应再调一次
    v3 = embed_mod.embed("different query", dim=32)
    assert calls["n"] == 2
    # 缓存命中信息
    info = embed_mod._embed_cached.cache_info()
    assert info.hits >= 1


def test_retrieval_cached(tmp_path):
    from hero_quant.memory.store import MemoryStore

    ms = MemoryStore(tmp_path / "cache_test")
    ms.write("k1", "贵州茅台 财报超预期 2026")
    ms.write("k2", "宁德时代 锂电池 技术突破")
    # 首次检索
    r1 = ms.search("茅台")
    # 首次应命中
    assert len(r1) >= 1
    # 模拟第二次同 query 命中缓存：应直接返回相同结果且不抛异常
    r2 = ms.search("茅台")
    assert r1 == r2
    # 缓存清空后仍可用
    ms.clear_retrieval_cache()
    r3 = ms.search("茅台")
    assert r3 == r1
    # vector_search 缓存
    v1 = ms.vector_search("茅台", top_k=5)
    v2 = ms.vector_search("茅台", top_k=5)
    assert v1 == v2

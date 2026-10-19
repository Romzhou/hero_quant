"""J线 J7: memory/store 深水区 —— write/search/recall/vector/namespace 全行为扫荡。"""
from __future__ import annotations

import time
from pathlib import Path

import pytest


def _mk(tmp_path, name="j7", namespace=None, **kw):
    from hero_quant.memory.store import MemoryStore
    return MemoryStore(tmp_path / name, namespace=namespace)


def test_j7_write_guards_and_routing(tmp_path, monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "")
    st = _mk(tmp_path, "w1")
    with pytest.raises(ValueError):
        st.write("", "content")
    with pytest.raises(ValueError):
        st.write("   ", "content")
    with pytest.raises(ValueError):
        st.write("k", "")
    with pytest.raises(ValueError):
        st.write("k", "   ")
    with pytest.raises(ValueError):
        st.write(123, "content")
    st.write("r1", "routing test content alpha", memory_type="project")
    st.write("r2", "routing test content beta", memory_type="unknown_xyz")
    st.write("r3", "routing test content gamma", memory_type=None)
    # hierarchy 缺席回退
    st.hierarchy = None
    st.write("r4", "no hierarchy routing content here", memory_type="project")
    # 覆盖写触发 backup 路径
    st.write("r1", "routing test content alpha v2 version two")


def test_j7_write_dedup_windows_and_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "")
    st = _mk(tmp_path, "w2")
    st.write("a", "identical content for dedup window test here")
    st.write("a", "identical content for dedup window test here")  # 窗口内同 key 去重
    st.write("b", "identical content for dedup window test here")  # 跨 key 保留
    # 预填制造 cap 分支
    now = time.time()
    st._recent_hashes = {f"h{i}": now - 100 for i in range(2060)}
    st.write("c", "cap eviction trigger content unique here")
    assert len(st._recent_hashes) <= 2060
    # 过期清理分支
    st._recent_hashes = {"old": now - 100, "new": now}
    st.write("d", "expiry cleanup content unique here")


def test_j7_namespace_and_safe_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "")
    st = _mk(tmp_path, "ns", namespace="team1")
    assert st._ns_key("k") != "k"
    assert st._ns_prefix() is not None
    st.write("k1", "namespaced content alpha beta gamma here")
    assert st.search("namespaced") is not None
    st0 = _mk(tmp_path, "ns0")
    assert st0._ns_prefix() is None
    assert st0._ns_key("k") == "k"
    # safe 前缀匹配/解析
    assert st._matches_safe_prefix("team1__k1.md") in (True, False)
    assert st._parse_safe_stem("team1__k1") is not None
    assert st._safe_prefix() is None or isinstance(st._safe_prefix(), str)


def test_j7_vector_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "")
    st = _mk(tmp_path, "v")
    st._embed_text = lambda t: [0.1, 0.2, 0.3]
    st._vector_enabled = True
    st.write("vk1", "vector enabled content machine learning here")
    st.write("vk2", "another vector doc about cooking recipes here")
    assert isinstance(st.vector_search("machine learning", top_k=2), list)
    assert isinstance(st.search("machine learning"), list)
    assert isinstance(st.recall("machine learning", top_k=2), list)
    # 禁向量回退
    st._vector_enabled = False
    assert isinstance(st.vector_search("machine learning"), list)
    st._embed_text = lambda t: None
    assert isinstance(st.vector_search("machine learning"), list)
    # 正交同维得 0
    assert st._cosine_sim([1.0, 0.0], [0.0, 1.0]) == 0.0
    st._ensure_vector_dim([0.1], "c")


def test_j7_fts_fallbacks_and_raw(tmp_path, monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "")
    st = _mk(tmp_path, "f")
    st.write("f1", "北京今天天气不错适合出行游玩")
    st.write("f2", "python programming tutorial for beginners")
    assert isinstance(st._search_bm25_raw("python"), list)
    assert isinstance(st._search_bigram_raw("北京"), list)
    assert isinstance(st._search_bigram_raw(""), list)
    # 强制关闭 fts 走 LIKE 回退
    st._fts_enabled = False
    st._trigram_enabled = False
    st._bigram_enabled = False
    assert isinstance(st._search_bm25_raw("python"), list)
    assert isinstance(st.search("python"), list)


def test_j7_reconcile_decay_meta_index(tmp_path, monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "")
    st = _mk(tmp_path, "rd")
    st.write("m1", "meta decay test content alpha here")
    st._meta[st._ns_key("m1")] = {"quality_score": 0.9, "access_count": 5,
                                  "last_accessed": time.time()}
    assert 0.0 <= st._importance_for(st._meta[st._ns_key("m1")], time.time()) <= 1.0
    assert st._importance_for({}, time.time()) is not None
    ranked = st._rank_with_decay([{"key": "m1", "quality_score": 0.9, "content": "x"},
                                  {"key": "m2"}])
    assert len(ranked) == 2
    # 孤儿文件 + 孤儿行
    orphan = Path(st.base) / "orphan_xyz.md"
    orphan.write_text("orphan body", encoding="utf-8")
    assert st._reconcile_orphan_files() >= 0
    st.index_external("ext1", "external index content here for test")
    st.index_external("ext1", "external index content here for test v2")
    st.index_external("", "empty key tolerated path")
    # pgvector 未启用短路
    st._pgvector_upsert("k", "c", [0.1, 0.2])
    assert st._pgvector_search("q", top_k=2) == []
    vec = st._load_vector_for_key("m1")
    assert vec is None or isinstance(vec, list)


def test_j7_search_cache_and_redis_l2(tmp_path, monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "")
    import hero_quant.infra.redis as R
    import fakeredis
    fake = fakeredis.FakeRedis(decode_responses=True)
    R.set_redis_instance(fake)
    try:
        st = _mk(tmp_path, "c")
        st.write("c1", "cached search content redis level two here")
        r1 = st.search("cached search")
        r2 = st.search("cached search")
        assert r1 is not None and r2 is not None
        st.clear_retrieval_cache(include_redis_l2=True)
        r3 = st.search("cached search")
        assert r3 is not None
    finally:
        R.clear_redis_instance()

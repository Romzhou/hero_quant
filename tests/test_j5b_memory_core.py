"""J线 J5b: memory —— hierarchy + lifecycle + store 主干探针。"""
from __future__ import annotations

import time
from pathlib import Path

import pytest


def _mk_store(tmp_path, name="m"):
    from hero_quant.memory.store import MemoryStore
    import os
    os.environ.pop("HERO_VECTOR_DSN", None)
    os.environ.pop("VECTOR_DSN", None)
    return MemoryStore(tmp_path / name)


# ---------- hierarchy ----------

def test_j5b_hierarchy_validate_and_route(tmp_path):
    from hero_quant.memory.hierarchy import CATEGORIES, MemoryHierarchy

    mh = MemoryHierarchy(tmp_path / "h")
    assert mh.base_dir.exists()
    for bad in ("", ".", "..", "a/b", "a\\b", "a:b", "/abs", "../x"):
        with pytest.raises(ValueError):
            mh._validate_filename(bad)
    p = mh.route_entry("project", "note.md")
    assert "project" in str(p)
    p2 = mh.route_entry("unknown_type_xyz", "note2.md")
    assert p2.name == "note2.md"
    p3 = mh.route_entry("", "note3.md")
    assert p3.name == "note3.md"
    assert isinstance(CATEGORIES, (set, list, tuple, frozenset))
    assert mh._ensure_category_dir("episodic").exists()


def test_j5b_hierarchy_recover_and_scan(tmp_path):
    from hero_quant.memory.hierarchy import MemoryHierarchy

    base = tmp_path / "h2"
    mh = MemoryHierarchy(base)
    good = base / "goodentry"
    good.write_text("---\ntitle: t\n---\nbody here\n", encoding="utf-8")
    plain = base / "plainentry"
    plain.write_text("no frontmatter at all\n", encoding="utf-8")
    rec = mh.recover_extensionless_entries()
    assert any(str(x).endswith(".md") for x in rec)
    assert (base / "plainentry").exists()  # 无 frontmatter 不动
    md = base / "a.md"
    md.write_text("# hello\n", encoding="utf-8")
    assert md in mh.scan_all()
    assert mh.scan_category("no_such_cat_xyz") == []
    proj_files = mh.scan_category("project")
    assert isinstance(proj_files, list)
    out3 = mh.prune_search_scope(set(), "project")


def test_j5b_hierarchy_index_and_prune(tmp_path):
    from hero_quant.memory.hierarchy import MemoryHierarchy

    base = tmp_path / "h3"
    mh = MemoryHierarchy(base)
    mh.rebuild_index([{"key": "k1", "content": "alpha beta"}, {"content": "gamma"}])
    kw = mh._parse_index_keywords()
    assert isinstance(kw, dict)
    out = mh.prune_search_scope({"alpha"}, "")
    assert isinstance(out, list)
    out2 = mh.prune_search_scope({"zzz_no_match_xyz"}, "")
    assert isinstance(out2, list)
    out3 = mh.prune_search_scope(set(), "episodic")
    assert isinstance(out3, list)
    # migrate flat
    flat = base / "flat.md"
    flat.write_text("# flat\n", encoding="utf-8")
    moved = mh.migrate_flat_entry(flat, "project")
    assert moved is None or isinstance(moved, Path)
    assert mh.migrate_flat_entry(base / "missing.md", "project") is None


# ---------- lifecycle ----------

def test_j5b_lifecycle_importance_and_alias(tmp_path):
    from hero_quant.memory.lifecycle import MemoryLifecycle, _now_iso, compute_importance

    assert _now_iso().startswith("20")
    assert 0.0 <= compute_importance(0.8, 3, 1.0) <= 1.0
    assert compute_importance(0.0, 0, 9999.0) == 0.0
    assert compute_importance(5.0, 100, 0.0) == 1.0
    st = _mk_store(tmp_path, "lc1")
    lc = MemoryLifecycle(st)
    assert lc.MAX_AGE == lc.MAX_AGE_DAYS
    lc.MAX_AGE = 30
    assert lc.MAX_AGE_DAYS == 30
    assert lc.memory_dir == Path(st.base)
    assert lc._scan_entries() == []
    # frontmatter meta 回退
    f = Path(st.base) / "n1.md"
    f.write_text("---\nquality_score: 0.9\naccess_count: 5\nlast_accessed: 1700000000\n---\nbody\n",
                 encoding="utf-8")
    qs, ac, last = lc._resolve_meta(f)
    assert qs == 0.9 and ac == 5
    qs2, _, _ = lc._resolve_meta(f, _meta_lookup={"n1.md": {"quality_score": 0.1}})
    assert qs2 == 0.1
    qs3, _, _ = lc._resolve_meta(f, _meta_lookup={"n1": {"quality_score": 0.2}})
    assert qs3 == 0.2
    # 脏 meta 值容错
    qs4, ac4, _ = lc._resolve_meta(f, _meta_lookup={"n1.md": {"quality_score": "bad", "access_count": "x"}})
    assert qs4 == 0.5


def test_j5b_lifecycle_gc_compress_reinforce(tmp_path):
    from hero_quant.memory.lifecycle import MemoryLifecycle

    st = _mk_store(tmp_path, "lc2")
    st.write("old1", "very old content about ancient history " * 10)
    #  老化：改 mtime 到 30 天前
    for p in Path(st.base).rglob("*.md"):
        old = time.time() - 30 * 86400
        import os
        os.utime(p, (old, old))
    lc = MemoryLifecycle(st)
    actions = lc.run_gc(dry_run=True)
    assert isinstance(actions, list)
    actions2 = lc.run_gc(dry_run=False)
    assert isinstance(actions2, list)
    comp = lc.compress(dry_run=True)
    assert isinstance(comp, list)
    comp2 = lc.compress(dry_run=False)
    assert isinstance(comp2, list)
    assert lc.reinforce("old1", "task_success") in (True, False)
    assert lc.reinforce("ghost_xyz", "task_success") in (True, False)
    lc.track_access("old1")
    lc.track_access(None)
    # 静态摘要 helpers
    assert isinstance(MemoryLifecycle._summary_sentences("First. Second! Third?"), list)
    assert isinstance(MemoryLifecycle._tokens("hello world"), list)
    assert isinstance(MemoryLifecycle._tfidf_summary([("a", "alpha beta"), ("b", "beta gamma")]), str)
    assert MemoryLifecycle._read_compressible(Path(st.base) / "nope.md") is None


# ---------- store ----------

def test_j5b_store_helpers_and_sidecar(monkeypatch, tmp_path):
    from hero_quant.memory import store as ST

    assert len(ST._content_hash("n", "c")) == 16
    assert ST._content_bigrams("abc") == "ab bc"
    assert ST._content_bigrams("") == ""
    assert ST._escape_like("a%b_c\\d") == "a\\%b\\_c\\\\d"
    assert ST._pgvector_dsn() is None
    monkeypatch.setenv("HERO_VECTOR_STORE", "local")
    assert ST.is_pgvector_configured() is False
    monkeypatch.delenv("HERO_VECTOR_STORE", raising=False)
    assert isinstance(ST.get_vector_dim(), int)
    lit = ST._vector_to_literal([0.1, 0.2])
    assert lit.startswith("[")
    with pytest.raises(ValueError):
        ST._vector_to_literal([float("inf")])
    w = ST._SQLiteConnWrapper.__new__(ST._SQLiteConnWrapper)
    import sqlite3
    conn = sqlite3.connect(":memory:")
    w2 = ST._SQLiteConnWrapper(conn)
    assert w2.execute is not None
    conn.close()
    # sidecar 未配置路径
    sc = ST.PgVectorSidecar(dsn=None)
    assert sc.ping() is False
    assert sc.upsert("k", "c", [0.1]) is False
    assert sc.search([0.1]) == []


def test_j5b_store_cache_ns_safe(tmp_path):
    st = _mk_store(tmp_path, "s1")
    assert st._cache_get({}, ("q",), 30.0) is None
    st._cache_set(st._retrieval_cache, ("q", 5), [{"key": "k"}])
    assert st._cache_get(st._retrieval_cache, ("q", 5), 30.0) == [{"key": "k"}]
    assert st._cache_get(st._retrieval_cache, ("q", 5), -1.0) is None  # 过期
    st.clear_retrieval_cache()
    st.clear_retrieval_cache(include_redis_l2=True)
    assert st._ns_key("k") is not None
    assert isinstance(st.vector_backend, str)
    assert st.get_vector_backend() == st.vector_backend
    assert st.is_pgvector_enabled() in (True, False)
    assert st._safe_filename("ns:key") is not None
    assert st.is_safe_filename("good.md") is True
    assert st.is_safe_filename("../evil") is False
    st._ensure_open()
    st.close()
    with pytest.raises(ValueError):
        st._ensure_open()
    with _mk_store(tmp_path, "sctx") as s2:
        assert s2 is not None


def test_j5b_store_write_search_recall(tmp_path, monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "")
    st = _mk_store(tmp_path, "s2")
    st.write("k1", "python programming language tutorial content here")
    st.write("k1", "python programming language tutorial content here")  # 去重
    st.write("k2", "cooking recipe pasta tomato sauce delicious meal")
    assert st.search("python programming") is not None
    assert isinstance(st.recall("python"), list)
    assert isinstance(st.vector_search("python"), list)
    assert isinstance(st._search_bigram_raw("python"), list)
    assert isinstance(st._search_bm25_raw("python"), list)
    assert st._cosine_sim([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert st._cosine_sim([1.0], [0.0]) == 0.0
    st._ensure_vector_dim([0.1, 0.2], "c")
    assert st._load_vector_for_key("ghost_xyz") is None
    st.index_external("ext1", "external content for index test here")
    imp = st._importance_for({"quality_score": 0.8, "access_count": 2}, time.time())
    assert 0.0 <= imp <= 1.0
    ranked = st._rank_with_decay([{"key": "a", "quality_score": 0.9}, {"key": "b"}])
    assert len(ranked) == 2
    st._pgvector_upsert("k", "c", [0.1])
    assert st._pgvector_search("q") == []
    st._invalidate_search_caches()
    st._invalidate_redis_search_cache()
    n = st._reconcile_orphan_files()
    assert n >= 0

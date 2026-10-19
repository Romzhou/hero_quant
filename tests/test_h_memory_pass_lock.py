"""H线：memory/ 裸 pass 行为锁定（只读 src，行为断言，不依赖行号）。

覆盖 lifecycle / hierarchy / store / rerank / ingest 的 offline-safe
回退分支：异常时不抛、回落到安全默认值，且可观测（warning/debug）
或语义正确（去重/隔离/回退排序）。
"""
import os
import time

import pytest


def _new_store(tmp_path, ns=None):
    from hero_quant.memory.store import MemoryStore

    ms = MemoryStore(str(tmp_path), namespace=ns)
    return ms


def _close(ms):
    try:
        ms.close()
    except Exception:
        pass


# ---------- lifecycle.compute_importance 边界 ----------

def test_h_compute_importance_zero_quality():
    from hero_quant.memory.lifecycle import compute_importance

    assert compute_importance(0.0, 5, 0.0) == 0.0


def test_h_compute_importance_negative_days_clamped():
    from hero_quant.memory.lifecycle import compute_importance

    a = compute_importance(0.8, 0, -10.0)
    b = compute_importance(0.8, 0, 0.0)
    assert a == pytest.approx(b)


def test_h_compute_importance_access_bonus_capped():
    from hero_quant.memory.lifecycle import compute_importance

    # access_bonus 上限 0.3：100 次访问与 3 次访问增益一致（同 quality/天数）
    assert compute_importance(0.5, 100, 0.0) == pytest.approx(compute_importance(0.5, 3, 0.0))


def test_h_compute_importance_range():
    from hero_quant.memory.lifecycle import compute_importance

    for q, ac, d in [(1.0, 0, 0.0), (0.5, 2, 7.0), (1.0, 10, 365.0), (0.0, 0, 0.0)]:
        v = compute_importance(q, ac, d)
        assert 0.0 <= v <= 1.0


# ---------- lifecycle offline-safe 分支 ----------

def test_h_lifecycle_scan_fallback_when_hierarchy_broken(tmp_path, monkeypatch):
    """hierarchy 抛错时 _scan_entries 回退到 rglob，不抛（lifecycle:101 pass 分支）。"""
    import hero_quant.memory.lifecycle as lc

    ms = _new_store(tmp_path / "ms")
    try:
        ms.write("k1", "lifecycle fallback content alpha")
        # 造一个 hierarchy.scan_all 抛错的替身
        import hero_quant.memory.hierarchy as hier

        class _Boom(hier.MemoryHierarchy):
            def scan_all(self):
                raise RuntimeError("boom")

        monkeypatch.setattr(hier, "MemoryHierarchy", _Boom)
        mgr = lc.MemoryLifecycle(ms)
        entries = mgr._scan_entries()
        assert isinstance(entries, list)
        assert len(entries) >= 1
    finally:
        _close(ms)


def test_h_lifecycle_resolve_meta_bad_frontmatter(tmp_path):
    """坏 frontmatter（非数值 quality）回落默认值，不抛（125/129/133 pass）。"""
    import hero_quant.memory.lifecycle as lc

    base_dir = tmp_path / "lc"
    base_dir.mkdir()
    p = base_dir / "bad.md"
    p.write_text("---\nquality_score: not_a_number\naccess_count: xx\nlast_accessed: nope\n---\nbody\n", encoding="utf-8")

    class _FakeMem:
        base = base_dir
        _meta = {}

        def _safe_filename(self, k):
            return f"{k}.md"

    mgr = lc.MemoryLifecycle(_FakeMem())
    qs, ac, last = mgr._resolve_meta(p)
    assert qs == pytest.approx(0.5)
    assert ac == 0
    assert last > 0


def test_h_lifecycle_resolve_meta_iso_timestamp(tmp_path):
    import hero_quant.memory.lifecycle as lc

    base_dir = tmp_path / "lc2"
    base_dir.mkdir()
    p = base_dir / "iso.md"
    p.write_text("---\nquality_score: 0.9\nlast_accessed: 2026-01-01T00:00:00Z\n---\nbody\n", encoding="utf-8")

    class _FakeMem:
        base = base_dir
        _meta = {}

        def _safe_filename(self, k):
            return f"{k}.md"

    mgr = lc.MemoryLifecycle(_FakeMem())
    qs, ac, last = mgr._resolve_meta(p)
    assert qs == pytest.approx(0.9)
    assert last < time.time()


def test_h_lifecycle_gc_young_files_skipped(tmp_path):
    """7 天龄门限：新文件不 GC（行为锁）。"""
    import hero_quant.memory.lifecycle as lc

    ms = _new_store(tmp_path / "gcyoung")
    try:
        ms.write("fresh", "fresh content that should not be gc-ed because it is young " * 3)
        mgr = lc.MemoryLifecycle(ms)
        actions = mgr.run_gc(dry_run=True)
        names = [a["name"] for a in actions]
        # 新文件 stem 可能被安全编码，但不应出现 fresh 相关动作
        assert not any("fresh" in n for n in names)
    finally:
        _close(ms)


def test_h_lifecycle_gc_old_low_quality_archived(tmp_path):
    """低质量 + 足龄 => archive（dry_run 不落盘）。"""
    import hero_quant.memory.lifecycle as lc

    ms = _new_store(tmp_path / "gcold")
    try:
        ms.write("oldlow", "old low quality content " * 5)
        # 回写低质量 meta + 老 last_accessed（60 天前）
        ms._meta["oldlow"] = {"quality_score": 0.01, "access_count": 0, "last_accessed": time.time() - 60 * 86400}
        # 文件 mtime 同样做旧 10 天
        for f in ms.base.rglob("*.md"):
            if "oldlow" in f.name or "oldlow" in f.stem:
                old = time.time() - 10 * 86400
                os.utime(f, (old, old))
        mgr = lc.MemoryLifecycle(ms)
        actions = mgr.run_gc(dry_run=True)
        assert any(a["action"] == "archive" for a in actions)
    finally:
        _close(ms)


def test_h_lifecycle_reinforce_unknown_event_false(tmp_path):
    import hero_quant.memory.lifecycle as lc

    ms = _new_store(tmp_path / "rf")
    try:
        mgr = lc.MemoryLifecycle(ms)
        assert mgr.reinforce("k", "no_such_event") is False
    finally:
        _close(ms)


def test_h_lifecycle_reinforce_capped(tmp_path):
    import hero_quant.memory.lifecycle as lc

    ms = _new_store(tmp_path / "rf2")
    try:
        ms.write("cap", "capped content " * 4)
        mgr = lc.MemoryLifecycle(ms)
        for _ in range(20):
            assert mgr.reinforce("cap", "user_confirm") is True
        assert abs(mgr._session_deltas["cap"]) <= 0.5 + 1e-9
    finally:
        _close(ms)


def test_h_lifecycle_track_access_noop(tmp_path):
    import hero_quant.memory.lifecycle as lc

    ms = _new_store(tmp_path / "ta")
    try:
        mgr = lc.MemoryLifecycle(ms)
        assert mgr.track_access(object()) is None
    finally:
        _close(ms)


def test_h_lifecycle_compress_dry_run_empty(tmp_path):
    import hero_quant.memory.lifecycle as lc

    ms = _new_store(tmp_path / "cmp")
    try:
        mgr = lc.MemoryLifecycle(ms)
        out = mgr.compress(dry_run=True)
        assert isinstance(out, list)
    finally:
        _close(ms)


# ---------- hierarchy 回退分支 ----------

def test_h_hierarchy_unknown_type_falls_back(tmp_path):
    from hero_quant.memory.hierarchy import MemoryHierarchy

    h = MemoryHierarchy(tmp_path)
    p = h.route_entry("weird_type_xyz", "note.md")
    assert p.parent.resolve() == tmp_path.resolve()


def test_h_hierarchy_rebuild_index_foreign_keywords(tmp_path):
    from hero_quant.memory.hierarchy import MemoryHierarchy

    h = MemoryHierarchy(tmp_path)
    h.rebuild_index([
        {"memory_type": "user", "keywords": ["OK", None, 123, "  ", "ok"]},
        {"memory_type": "nope", "keywords": ["x"]},
        "not-a-dict",
    ])
    data_p = tmp_path / ".hierarchy.yaml"
    assert data_p.is_file()


def test_h_hierarchy_parse_index_missing_returns_empty(tmp_path):
    from hero_quant.memory.hierarchy import MemoryHierarchy

    h = MemoryHierarchy(tmp_path / "noidx")
    assert h._parse_index_keywords() == {}


def test_h_hierarchy_parse_index_bad_yaml_falls_back(tmp_path):
    from hero_quant.memory.hierarchy import MemoryHierarchy

    h = MemoryHierarchy(tmp_path)
    (tmp_path / ".hierarchy.yaml").write_text("user:\n  keywords: [a, b\n", encoding="utf-8")
    out = h._parse_index_keywords()
    assert isinstance(out, dict)


def test_h_hierarchy_prune_no_index_scans_all(tmp_path):
    from hero_quant.memory.hierarchy import MemoryHierarchy

    h = MemoryHierarchy(tmp_path)
    (tmp_path / "a.md").write_text("# t\nhello\n", encoding="utf-8")
    out = h.prune_search_scope({"zzz_no_match_qqq"})
    assert any(p.name == "a.md" for p in out)


def test_h_hierarchy_prune_unknown_filter_empty(tmp_path):
    from hero_quant.memory.hierarchy import MemoryHierarchy

    h = MemoryHierarchy(tmp_path)
    assert h.prune_search_scope({"x"}, category_filter="nope_cat") == []


def test_h_hierarchy_prune_known_filter_scoped(tmp_path):
    from hero_quant.memory.hierarchy import MemoryHierarchy

    h = MemoryHierarchy(tmp_path)
    (tmp_path / "user").mkdir(exist_ok=True)
    (tmp_path / "user" / "u1.md").write_text("# u\nhello\n", encoding="utf-8")
    out = h.prune_search_scope({"anything"}, category_filter="user")
    assert any(p.name == "u1.md" for p in out)


def test_h_hierarchy_migrate_rejects(tmp_path):
    from hero_quant.memory.hierarchy import MemoryHierarchy

    h = MemoryHierarchy(tmp_path)
    assert h.migrate_flat_entry(tmp_path / "missing.md", "user") is None
    evil = tmp_path / "a__b.md"
    evil.write_text("x", encoding="utf-8")
    # 非 base parent（子目录文件）应拒绝迁移
    sub = tmp_path / "sub"
    sub.mkdir()
    f2 = sub / "f.md"
    f2.write_text("x", encoding="utf-8")
    assert h.migrate_flat_entry(f2, "user") is None
    # 非法类别拒绝
    assert h.migrate_flat_entry(evil, "nope") is None


# ---------- store offline-safe 分支 ----------

def test_h_store_helpers_deterministic():
    from hero_quant.memory.store import _content_hash, _content_bigrams, _escape_like

    assert _content_hash("a", "hello") == _content_hash("A", "HELLO")
    assert isinstance(_content_bigrams("hello world"), str)
    assert _escape_like("100%_\\") == "100\\%\\_\\\\"


def test_h_store_vector_literal_finite_and_inf():
    from hero_quant.memory.store import _vector_to_literal

    lit = _vector_to_literal([0.1, 0.2, 0.3])
    assert lit.startswith("[") and lit.endswith("]")
    with pytest.raises(ValueError):
        _vector_to_literal([float("inf"), 0.0])
    with pytest.raises(ValueError):
        _vector_to_literal([float("nan")])


def test_h_store_pgvector_disabled_by_default():
    from hero_quant.memory.store import is_pgvector_configured

    # 默认无 DSN（且 vector_store=local/offline 时）应为 False，不抛
    assert is_pgvector_configured() in (True, False)


def test_h_store_pgvector_sidecar_disabled_noop(tmp_path):
    from hero_quant.memory.store import PgVectorSidecar

    sc = PgVectorSidecar(dsn=None, dim=32)
    assert sc.ping() is False
    assert sc.upsert("k", "c", [0.1] * 32) is False
    assert sc.search([0.1] * 32) == []


def test_h_store_write_dedup_and_close_guard(tmp_path):
    ms = _new_store(tmp_path / "w")
    try:
        ms.write("dup", "same content here")
        ms.write("dup", "same content here")
        cur = ms._conn.cursor()
        cur.execute("SELECT COUNT(*) FROM notes")
        assert cur.fetchone()[0] == 1
        ms.close()
        ms.close()  # 幂等
        with pytest.raises(ValueError):
            ms.write("after_close", "should fail fast")
    finally:
        _close(ms)


def test_h_store_cosine_zero_vectors(tmp_path):
    ms = _new_store(tmp_path / "cos")
    try:
        assert ms._cosine_sim([0.0] * 4, [0.0] * 4) == 0.0
        v = ms._cosine_sim([1.0, 0.0], [1.0, 0.0])
        assert v == pytest.approx(1.0)
    finally:
        _close(ms)


def test_h_store_embed_fallback_dim(tmp_path):
    from hero_quant.memory.store import get_vector_dim

    ms = _new_store(tmp_path / "emb")
    try:
        vec = ms._embed_text("hello world")
        assert isinstance(vec, list) and len(vec) == get_vector_dim()
    finally:
        _close(ms)


def test_h_store_pgvector_helpers_no_raise(tmp_path):
    ms = _new_store(tmp_path / "pghelp")
    try:
        ms._pgvector_upsert("k", "content", None)  # vec None 直接回退
        assert ms._pgvector_search("") == []
        assert ms.vector_backend in ("local", "pgvector")
        assert ms.get_vector_backend() in ("local", "pgvector")
        assert ms.is_pgvector_enabled() in (True, False)
    finally:
        _close(ms)


def test_h_store_search_cache_clear(tmp_path):
    ms = _new_store(tmp_path / "cc")
    try:
        ms.write("k1", "cache clear isolation content alpha beta")
        r1 = ms.search("cache clear")
        assert len(r1) >= 1
        ms.clear_retrieval_cache()
        r2 = ms.search("cache clear")
        assert len(r2) >= 1
    finally:
        _close(ms)


# ---------- rerank 回退分支 ----------

def test_h_rerank_no_key_fallback_sorted():
    from hero_quant.memory import rerank

    r = rerank.CohereReranker(api_key="")
    before = rerank.get_fallback_count()
    out = r.rerank("q", [("b", 0.1), ("a", 0.9)])
    assert out[0][0] == "a"
    assert rerank.get_fallback_count() >= before + 1


def test_h_rerank_empty_returns_empty():
    from hero_quant.memory.rerank import CohereReranker

    r = CohereReranker(api_key="")
    assert r.rerank("q", []) == []


def test_h_rerank_invalid_timeout_and_topk():
    from hero_quant.memory.rerank import CohereReranker

    with pytest.raises(ValueError):
        CohereReranker(api_key="", timeout=0)
    with pytest.raises(ValueError):
        CohereReranker(api_key="", timeout="bad")
    r = CohereReranker(api_key="dummy-key-for-validation")
    with pytest.raises(ValueError):
        r.rerank("q", [("a", 1.0)], top_k=0)
    with pytest.raises(ValueError):
        r.rerank("q", [("a", 1.0)], top_k="bad")


def test_h_rerank_dict_candidates_normalized():
    from hero_quant.memory.rerank import CohereReranker

    r = CohereReranker(api_key="")
    out = r.rerank("q", [{"key": "k1", "score": "bad"}, {"id": "k2"}])
    keys = [k for k, _ in out]
    assert "k1" in keys and "k2" in keys


# ---------- ingest 边界 ----------

def test_h_ingest_heading_fence_not_split():
    from hero_quant.memory.ingest import _split_by_heading

    text = "# real\nbody\n```\n# not heading\n```\n# second\nmore\n"
    secs = _split_by_heading(text)
    assert len(secs) == 2
    assert secs[0].startswith("# real")


def test_h_ingest_overlap_invalid_args():
    from hero_quant.memory.ingest import _overlap_chunks
    import pytest as _pt

    with _pt.raises(ValueError):
        _overlap_chunks("abc", chunk=0)
    with _pt.raises(ValueError):
        _overlap_chunks("abc", chunk=4, overlap=4)
    assert _overlap_chunks("") == []
    assert _overlap_chunks("ab", chunk=10, overlap=0) == ["ab"]


def test_h_ingest_missing_file_raises(tmp_path):
    from hero_quant.memory.ingest import ingest_markdown

    with pytest.raises(FileNotFoundError):
        ingest_markdown(tmp_path / "nope.md", store=object())


def test_h_ingest_empty_markdown_zero(tmp_path):
    from hero_quant.memory.ingest import ingest_markdown

    ms = _new_store(tmp_path / "ingstore")
    try:
        p = tmp_path / "empty.md"
        p.write_text("   \n", encoding="utf-8")
        assert ingest_markdown(p, store=ms) == 0
    finally:
        _close(ms)


def test_h_ingest_roundtrip_chunks(tmp_path):
    from hero_quant.memory.ingest import ingest_markdown

    ms = _new_store(tmp_path / "ing2")
    try:
        p = tmp_path / "doc.md"
        p.write_text("# A\n" + "alpha " * 200 + "\n# B\nbeta content here\n", encoding="utf-8")
        n = ingest_markdown(p, chunk=64, overlap=8, store=ms)
        assert n >= 2
    finally:
        _close(ms)

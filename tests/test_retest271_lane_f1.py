"""Lane F (retest271) repro tests — TDD red-first for 53 agent/memory findings.

Covers 12 files:
  hierarchy, graph, loop, prompt, agent container, agent buffer,
  agent store, ingest, lifecycle, rank_fusion, memory store, mcp router.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest


# ================= hierarchy (8) =================

def test_lane_f_hierarchy_rejects_unclosed_frontmatter(tmp_path):
    from hero_quant.memory.hierarchy import MemoryHierarchy
    base = tmp_path / "h"
    mh = MemoryHierarchy(base)
    cat = base / "user"
    cat.mkdir(parents=True, exist_ok=True)
    (cat / "note1").write_text("---\njust a horizontal rule doc\n", encoding="utf-8")
    out = mh.recover_extensionless_entries()
    assert (cat / "note1").exists(), f"unclosed --- must not be renamed, got {out}"
    assert not (cat / "note1.md").exists()


def test_lane_f_hierarchy_recovers_base_dir(tmp_path):
    from hero_quant.memory.hierarchy import MemoryHierarchy
    base = tmp_path / "h"
    mh = MemoryHierarchy(base)
    (base / "flat1").write_text("---\ntitle: x\n---\nbody\n", encoding="utf-8")
    mh.recover_extensionless_entries()
    assert (base / "flat1.md").exists(), "base_dir extensionless files must be recovered"


def test_lane_f_hierarchy_index_tracks_base_fallback(tmp_path):
    import yaml
    from hero_quant.memory.hierarchy import MemoryHierarchy
    base = tmp_path / "h"
    mh = MemoryHierarchy(base)
    mh.rebuild_index([{"memory_type": "", "keywords": ["basekw"]}])
    data = yaml.safe_load((base / ".hierarchy.yaml").read_text(encoding="utf-8"))
    blob = str(data)
    assert "base" in blob.lower() or "unknown" in blob.lower() or "fallback" in blob.lower(), \
        f"base fallback must be tracked in index, got {blob!r}"


def test_lane_f_hierarchy_parse_keywords_normalized():
    from hero_quant.memory.hierarchy import MemoryHierarchy
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    mh = MemoryHierarchy(tmp)
    (tmp / ".hierarchy.yaml").write_text(
        "rebuilt_at: x\ncategories:\n  user: {count: 1, keywords: [Python, None, 5]}\n"
        "  feedback: {count: 0, keywords: []}\n  project: {count: 0, keywords: []}\n"
        "  reference: {count: 0, keywords: []}\n", encoding="utf-8")
    try:
        out = mh._parse_index_keywords()
        assert out["user"] == ["python"], f"must lowercase + drop non-str, got {out['user']!r}"
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_lane_f_hierarchy_prune_case_insensitive(tmp_path):
    from hero_quant.memory.hierarchy import MemoryHierarchy
    base = tmp_path / "h"
    mh = MemoryHierarchy(base)
    mh.rebuild_index([{"memory_type": "user", "keywords": ["Python"]}])
    (base / "user").mkdir(parents=True, exist_ok=True)
    (base / "user" / "a.md").write_text("x", encoding="utf-8")
    scoped = mh.prune_search_scope({"Python"}, "")
    names = {p.name for p in scoped}
    assert "a.md" in names, f"mixed-case query must match lowercased index, got {names}"


def test_lane_f_hierarchy_rebuild_tmp_no_stale(tmp_path, monkeypatch):
    from hero_quant.memory.hierarchy import MemoryHierarchy
    base = tmp_path / "h"
    mh = MemoryHierarchy(base)
    import hero_quant.memory.hierarchy as H
    real_dump = H.yaml.safe_dump
    def boom(*a, **k):
        raise OSError("ENOSPC")
    monkeypatch.setattr(H.yaml, "safe_dump", boom)
    with pytest.raises(OSError):
        mh.rebuild_index([{"memory_type": "user", "keywords": []}])
    monkeypatch.setattr(H.yaml, "safe_dump", real_dump)
    leftovers = list(base.glob(".hierarchy*tmp*"))
    assert leftovers == [], f"stale tmp must be cleaned, got {leftovers}"


def test_lane_f_hierarchy_prune_includes_base(tmp_path):
    from hero_quant.memory.hierarchy import MemoryHierarchy
    base = tmp_path / "h"
    mh = MemoryHierarchy(base)
    mh.rebuild_index([{"memory_type": "user", "keywords": ["alpha"]}])
    (base / "user").mkdir(parents=True, exist_ok=True)
    (base / "user" / "a.md").write_text("x", encoding="utf-8")
    (base / "zzz.md").write_text("base fallback", encoding="utf-8")
    scoped = mh.prune_search_scope({"alpha"}, "")
    names = {p.name for p in scoped}
    assert "zzz.md" in names, f"filtered prune must include base fallback, got {names}"


def test_lane_f_hierarchy_allows_double_dot_name(tmp_path):
    from hero_quant.memory.hierarchy import MemoryHierarchy
    mh = MemoryHierarchy(tmp_path / "h")
    p = mh._validate_filename("a..b.md")
    assert p.name == "a..b.md"


# ================= graph (6) =================

def test_lane_f_ingest_external_key_disambiguated(tmp_path):
    from hero_quant.memory.store import MemoryStore
    from hero_quant.memory.ingest import ingest_markdown
    d1 = tmp_path / "d1"; d2 = tmp_path / "d2"
    d1.mkdir(); d2.mkdir()
    (d1 / "same.md").write_text("# T\nsame body\n", encoding="utf-8")
    (d2 / "same.md").write_text("# T\nsame body\n", encoding="utf-8")
    ms = MemoryStore(tmp_path / "mem")
    ingest_markdown(d1 / "same.md", store=ms)
    ingest_markdown(d2 / "same.md", store=ms)
    cur = ms._conn.cursor()
    cur.execute("SELECT COUNT(*) FROM notes")
    assert cur.fetchone()[0] == 2, "same-name different-dir chunks must not collide"


def test_lane_f_ingest_crlf_offsets(tmp_path):
    from hero_quant.memory.ingest import _split_by_heading
    text = "# A\r\nbody a\r\n# B\r\nbody b\r\n"
    secs = _split_by_heading(text)
    assert any(s.startswith("# A") and "body a" in s for s in secs), f"CRLF mis-slice: {secs!r}"
    assert any(s.startswith("# B") and "body b" in s for s in secs), f"CRLF mis-slice: {secs!r}"


def test_lane_f_ingest_settings_fallback_logs(tmp_path, caplog, monkeypatch):
    import hero_quant.memory.ingest as ing
    import hero_quant.config.settings as S
    monkeypatch.setattr(S, "get_settings", lambda: (_ for _ in ()).throw(RuntimeError("cfg boom")))
    p = tmp_path / "doc.md"
    p.write_text("# T\nbody\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        try:
            ing.ingest_markdown(p)
        except RuntimeError:
            pass
    assert any("memory" in r.message.lower() or "fall" in r.message.lower() for r in caplog.records)


def test_lane_f_ingest_no_fence_re():
    import hero_quant.memory.ingest as ing
    assert not hasattr(ing, "_FENCE_RE"), "dead _FENCE_RE must be removed"


def test_lane_f_ingest_no_line_offsets():
    src = Path("src/hero_quant/memory/ingest.py").read_text(encoding="utf-8")
    assert "line_offsets" not in src, "dead line_offsets must be removed"


# ================= lifecycle (5) =================

def test_lane_f_lifecycle_enforces_max_count(tmp_path):
    import time
    from hero_quant.memory.lifecycle import MemoryLifecycle
    base = tmp_path / "mem"; base.mkdir()
    class M:
        _meta = {}
        def _safe_filename(self, k):
            return f"{k}.md"
    for i in range(510):
        (base / f"n{i}.md").write_text("x", encoding="utf-8")
        old = time.time() - 40 * 86400
        import os
        os.utime(base / f"n{i}.md", (old, old))
    lc = MemoryLifecycle(M())
    actions = lc.run_gc(dry_run=True)
    assert len(actions) >= 10, f"count overflow beyond 500 must surface, got {len(actions)}"


def test_lane_f_lifecycle_delete_purges(tmp_path):
    import time
    from hero_quant.memory.lifecycle import MemoryLifecycle
    base = tmp_path / "mem"; base.mkdir()
    old = time.time() - 40 * 86400
    import os
    fp = base / "gone.md"; fp.write_text("secret", encoding="utf-8")
    os.utime(fp, (old, old))
    class M:
        def __init__(self):
            self._meta = {"gone": {"quality_score": 0.0, "access_count": 0,
                                   "last_accessed": time.time() - 40 * 86400}}
            self.base = base
        def _safe_filename(self, k):
            return f"{k}.md"
    lc = MemoryLifecycle(M())
    acts = [a for a in lc.run_gc(dry_run=False) if a["action"] == "delete"]
    assert acts, "expected a delete action"
    assert not (base / "archive" / "gone.md").exists(), "true delete must not retain copy"
    assert list((base / "archive").rglob("gone*")) == [], "no backup copy may survive"


def test_lane_f_lifecycle_compress_survives_missing_file(tmp_path):
    from hero_quant.memory.lifecycle import MemoryLifecycle
    base = tmp_path / "mem"; base.mkdir()
    (base / "a.md").write_text("alpha market trend. alpha signal useful.", encoding="utf-8")
    import time, os
    old = time.time() - 10 * 86400
    os.utime(base / "a.md", (old, old))
    class M:
        _meta = {}
        def __init__(self):
            self.base = base
        def _safe_filename(self, k):
            return f"{k}.md"
    lc = MemoryLifecycle(M())
    orig = lc._scan_entries
    def rigged():
        files = orig()
        # simulate concurrent deletion: unlink one before compress reads mtime
        for f in files:
            if f.name == "a.md":
                f.unlink(missing_ok=True)
        return files + [base / "ghost.md"]
    lc._scan_entries = rigged  # type: ignore
    lc.compress(dry_run=False)  # must not raise


def test_lane_f_lifecycle_naive_ts_utc(tmp_path):
    from hero_quant.memory.lifecycle import MemoryLifecycle
    base = tmp_path / "mem"; base.mkdir()
    (base / "n.md").write_text("---\nquality_score: 0.5\naccess_count: 0\nlast_accessed: 2024-01-01T00:00:00\n---\nbody\n", encoding="utf-8")
    class M:
        _meta = {}
        def _safe_filename(self, k):
            return f"{k}.md"
    lc = MemoryLifecycle(M())
    _, _, last = lc._resolve_meta(base / "n.md")
    from datetime import datetime, timezone
    expect = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
    assert abs(last - expect) < 1, f"naive ts must parse as UTC, got {last} vs {expect}"


def test_lane_f_lifecycle_reinforce_implemented():
    from hero_quant.memory.lifecycle import MemoryLifecycle
    class M:
        _meta = {}
        base = Path(".")
    lc = MemoryLifecycle(M())
    assert lc.reinforce("n", "task_success") is True
    assert lc.reinforce("n", "nope") is False


# ================= rank_fusion (2) =================

def test_lane_f_store_bigram_locked():
    src = Path("src/hero_quant/memory/store.py").read_text(encoding="utf-8")
    seg = src.split("def _search_bigram_raw")[1].split("def ")[0]
    assert "self._lock" in seg, "_search_bigram_raw must hold self._lock"


def test_lane_f_store_write_invalidates_redis(tmp_path):
    from hero_quant.memory.store import MemoryStore
    ms = MemoryStore(tmp_path / "mem")
    ms.write("k1", "redis invalidate probe alpha")
    ms.search("probe alpha")
    ms.write("k2", "redis invalidate probe alpha")
    src = Path("src/hero_quant/memory/store.py").read_text(encoding="utf-8")
    assert "hero:cache:memory:search" in src.split("def clear_retrieval_cache")[1].split("def ")[0], \
        "clear_retrieval_cache must invalidate Redis L2"
    ms.close()


def test_lane_f_store_reconciles_orphan_files(tmp_path):
    from hero_quant.memory.store import MemoryStore
    base = tmp_path / "mem"; base.mkdir()
    (base / "orphan.md").write_text("orphan content body", encoding="utf-8")
    ms = MemoryStore(base)
    hits = ms.search("orphan content")
    assert any("orphan" in h.get("content", "") for h in hits), "startup must reconcile files missing from DB"
    ms.close()


def test_lane_f_store_closed_guard(tmp_path):
    from hero_quant.memory.store import MemoryStore
    ms = MemoryStore(tmp_path / "mem")
    ms.write("k", "v body")
    ms.close()
    with pytest.raises(ValueError):
        ms.write("k2", "v2 body")


def test_lane_f_store_cross_key_same_content(tmp_path):
    from hero_quant.memory.store import MemoryStore
    ms = MemoryStore(tmp_path / "mem")
    ms.write("a", "shared body text")
    ms.write("b", "shared body text")
    cur = ms._conn.cursor()
    cur.execute("SELECT COUNT(*) FROM notes")
    assert cur.fetchone()[0] == 2, "distinct keys must not be dedup-dropped"
    ms.close()


# ================= router (6) =================


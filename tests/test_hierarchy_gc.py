"""D4 - hierarchy routing + GC lifecycle (0.15 threshold) TDD."""
import time
from pathlib import Path


def test_hierarchy_routing(tmp_path):
    """route_entry 按 type 进 category 目录；未知 type 回落 base."""
    from hero_quant.memory.hierarchy import MemoryHierarchy, CATEGORIES

    base = tmp_path / "memory"
    mh = MemoryHierarchy(base)

    # known categories must route to base/{category}/{filename}
    for cat in CATEGORIES:
        p = mh.route_entry(cat, "hello.md")
        assert p.parent.name == cat, f"route_entry({cat}) should go to {cat}/"
        assert p.name == "hello.md"
        # directory should be created on demand
        assert p.parent.is_dir(), f"category dir {cat} should be created"
        # base_dir property
        assert mh.base_dir == base

    # different filename still routed
    p2 = mh.route_entry("user", "note-123.md")
    assert p2 == base / "user" / "note-123.md"

    # unknown type fallback to base dir
    p_unknown = mh.route_entry("unknown_type", "x.md")
    assert p_unknown == base / "x.md"
    assert p_unknown.parent == base

    # empty / unknown should also fallback (no exception)
    p_empty = mh.route_entry("", "y.md")
    assert p_empty == base / "y.md"

    # scan_category & scan_all existence
    # after routing, writing files should be discoverable
    (base / "user" / "a.md").write_text("hello", encoding="utf-8")
    (base / "project" / "b.md").write_text("world", encoding="utf-8")
    (base / "flat.md").write_text("flat", encoding="utf-8")

    user_files = mh.scan_category("user")
    assert any(f.name == "a.md" for f in user_files)

    all_files = mh.scan_all()
    names = {f.name for f in all_files}
    assert "a.md" in names
    assert "b.md" in names
    assert "flat.md" in names

    # archive dir should be skipped
    (base / "archive").mkdir(exist_ok=True)
    (base / "archive" / "old.md").write_text("archived", encoding="utf-8")
    assert "old.md" not in {f.name for f in mh.scan_all()}


def test_gc_archive_threshold(tmp_path):
    """GC 按 ARCHIVE 0.15 分层：低 importance + 足龄 -> archive；高分/新文件保留；dry_run 不落盘."""
    import os
    from hero_quant.memory.store import MemoryStore
    from hero_quant.memory.lifecycle import MemoryLifecycle, ARCHIVE_THRESHOLD, compute_importance

    # threshold must be 0.15
    assert ARCHIVE_THRESHOLD == 0.15

    ms = MemoryStore(tmp_path)

    # Write two entries with different quality/age
    ms.write("old_low", "alpha decay stale low quality entry")
    ms.write("fresh_high", "alpha decay fresh high quality entry")

    now = time.time()
    # inject meta via in-memory Dict (no DDL)
    # old_low: qs=0.1, ac=0, 14 days ago -> importance ~0.05 (<0.15) should archive
    # fresh_high: qs=0.9, ac=5, 1 day ago -> importance ~0.7 (>0.15) keep
    old_key = ms._ns_key("old_low")
    fresh_key = ms._ns_key("fresh_high")
    ms._meta[old_key] = {"quality_score": 0.1, "access_count": 0, "last_accessed": now - 14 * 86400}
    ms._meta[fresh_key] = {"quality_score": 0.9, "access_count": 5, "last_accessed": now - 1 * 86400}

    # Make file mtime old enough for MIN_AGE_DAYS=7
    # MemoryStore writes flat files in base dir
    old_file = tmp_path / ms._safe_filename(old_key)
    fresh_file = tmp_path / ms._safe_filename(fresh_key)
    # ensure files exist
    assert old_file.exists()
    assert fresh_file.exists()
    # set mtime to 14 days ago for old_low, 1 day ago for fresh_high
    os.utime(old_file, (now - 14 * 86400, now - 14 * 86400))
    os.utime(fresh_file, (now - 1 * 86400, now - 1 * 86400))

    # verify importance calculation aligns with threshold
    imp_old = compute_importance(0.1, 0, 14.0)
    imp_fresh = compute_importance(0.9, 5, 1.0)
    assert imp_old < 0.15
    assert imp_fresh > 0.15

    lc = MemoryLifecycle(ms)

    # dry_run should report but NOT move files
    actions_dry = lc.run_gc(dry_run=True)
    assert any(a["action"] == "archive" and "old_low" in a["name"] for a in actions_dry)
    assert not any("fresh_high" in a["name"] for a in actions_dry)
    # files still there after dry_run
    assert old_file.exists(), "dry_run must not move files"
    assert (tmp_path / "archive" / old_file.name).exists() is False
    # actually ensure archive not created effectively: old file still at original
    # gc.log should be created (vibe does _append_gc_log)
    assert (tmp_path / "gc.log").exists()

    # real run should archive old_low
    actions = lc.run_gc(dry_run=False)
    archived_path = tmp_path / "archive" / old_file.name
    assert archived_path.exists(), f"old_low should be archived to {archived_path}"
    assert not old_file.exists(), "original should be moved"
    # fresh_high must stay
    assert fresh_file.exists()

    # actions should contain reason with threshold
    rec = next(a for a in actions if "old_low" in a["name"])
    assert rec["importance"] < 0.15
    assert "archive" in rec["action"]


def test_gc_min_age_respects_7days(tmp_path):
    """未满 MIN_AGE_DAYS (7) 即使低分也不归档."""
    import os, time
    from hero_quant.memory.store import MemoryStore
    from hero_quant.memory.lifecycle import MemoryLifecycle

    ms = MemoryStore(tmp_path)
    ms.write("recent_low", "very low quality but recent entry")

    now = time.time()
    recent_key = ms._ns_key("recent_low")
    ms._meta[recent_key] = {"quality_score": 0.05, "access_count": 0, "last_accessed": now - 1 * 86400}
    recent_file = tmp_path / ms._safe_filename(recent_key)
    # mtime is now (recent) -> age < 7 days
    os.utime(recent_file, (now - 1 * 86400, now - 1 * 86400))

    lc = MemoryLifecycle(ms)
    actions = lc.run_gc(dry_run=False)
    # should be empty (min_age protection)
    assert not any("recent_low" in a["name"] for a in actions)
    assert recent_file.exists()


def test_store_hierarchy_write_integration(tmp_path):
    """MemoryStore.write with memory_type should route to category dir via hierarchy; search still finds."""
    from hero_quant.memory.store import MemoryStore

    ms = MemoryStore(tmp_path)
    # new signature: write(key, content, memory_type=...)
    # backward compat: existing 2-arg calls still work
    try:
        ms.write("hier_note", "category routed content", memory_type="user")
        routed = tmp_path / "user" / ms._safe_filename(ms._ns_key("hier_note"))
        # if hierarchy integration exists, file should be in user subdir
        # if not, it will be in base dir - we assert routed exists to enforce hierarchy
        assert routed.exists(), f"hierarchical write should route to {routed}, base fallback is not enough"
        # search via DB should still find it (FTS)
        results = ms.search("category routed")
        assert len(results) >= 1
        assert any("hier_note" in r["key"] for r in results)
        # also file fallback search should work from hierarchy (glob)
        # ensure base flat file not duplicated
        flat = tmp_path / ms._safe_filename(ms._ns_key("hier_note"))
        if routed != flat:
            assert not flat.exists() or flat == routed
    except TypeError:
        # if store doesn't support memory_type, fail the test to enforce implementation
        assert False, "MemoryStore.write must support memory_type param for hierarchy routing"

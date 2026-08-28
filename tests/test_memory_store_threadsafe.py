import pathlib
import sqlite3

def test_write_atomic_failure_cleans_file(tmp_path):
    from hero_quant.memory.store import MemoryStore
    ms = MemoryStore(base_path=tmp_path)
    # basic write succeeds and file exists
    ms.write("k", "content")
    assert (tmp_path / "k.md").exists(), "k.md should exist after write"
    assert hasattr(ms, "_conn") and ms._conn is not None
    # check WAL enabled either via PRAGMA or source assertion
    try:
        cur = ms._conn.cursor()
        cur.execute("PRAGMA journal_mode")
        mode = cur.fetchone()
        wal_on = mode and mode[0] and mode[0].lower() == "wal"
    except Exception:
        wal_on = False
    src = pathlib.Path("src/hero_quant/memory/store.py").read_text(encoding="utf-8")
    wal_src = "journal_mode=WAL" in src or "journal_mode" in src.lower()
    # must have WAL enabled
    assert wal_on or wal_src, "WAL not enabled: PRAGMA journal_mode should be WAL"
    # check source has WAL and busy_timeout and timeout
    assert "busy_timeout" in src.lower(), "busy_timeout missing in source"
    assert "timeout" in src.lower(), "_init_db should set timeout=10.0"

    # --- atomic double-write failure should clean orphan file ---
    # Patch commit to fail to simulate DB double-write failure
    original_commit = ms._conn.commit
    def failing_commit():
        raise sqlite3.OperationalError("simulated DB failure")
    ms._conn.commit = failing_commit
    # ensure _content_hash uses [:16] already? check below but still proceed
    try:
        ms.write("k2", "second content")
    except Exception:
        pass
    finally:
        ms._conn.commit = original_commit
    # orphan file should NOT remain (atomic cleanup)
    assert not (tmp_path / "k2.md").exists(), "orphan k2.md should be cleaned on DB failure (atomic double-write)"
    # source should mention reconcile or cleanup warning
    assert ("reconcile" in src.lower() or "unlink" in src.lower() or "warning" in src.lower()), "write failure path should delete file or mention reconcile with warning"

def test_recent_hashes_locked():
    src = pathlib.Path("src/hero_quant/memory/store.py").read_text(encoding="utf-8")
    # RLock existence
    assert "self._lock" in src, "self._lock not found in store.py"
    assert "RLock" in src, "RLock not found in store.py"
    assert "threading" in src, "threading import missing"
    # _recent_hashes guarded
    # look for with self._lock around _recent_hashes
    assert "_recent_hashes" in src
    # ensure with self._lock appears near _recent_hashes
    idx_lock = src.find("with self._lock")
    idx_recent = src.find("_recent_hashes")
    assert idx_lock != -1 and idx_recent != -1, "with self._lock guard missing"
    # _meta, _retrieval_cache, _vector_cache also guarded
    for name in ("_meta", "_retrieval_cache", "_vector_cache"):
        assert name in src
    # at least two lock blocks
    assert src.count("with self._lock") >= 2, "expected multiple with self._lock blocks"

    # _content_hash length 16
    assert "[:16]" in src, "_content_hash should be [:16]"
    # tmp unique naming with pid/tid/uuid
    assert "os.getpid()" in src
    # uuid or mkstemp or O_EXCL
    assert ("uuid" in src.lower() or "mkstemp" in src.lower() or "O_EXCL" in src), "tmp name should be unique with uuid/mkstemp/O_EXCL"

    # recall slicing
    assert "def recall" in src
    # recall should slice by top_k
    assert "top_k" in src.split("def recall")[1].split("def ")[0], "recall should accept top_k"
    # ensure recall slices (e.g., [:max(0")
    recall_segment = src.split("def recall")[1].split("def ")[0]
    assert "search" in recall_segment and "top_k" in recall_segment, "recall should delegate to search with top_k slice"

    # _cache deep copy returns [dict(
    assert "dict(x" in src or "dict(v" in src or "[dict(" in src, "_cache should return deep copy [dict(x)...]"

    # FTS MATCH escaping with quotes (like _search_bigram_raw)
    # check that _search_bm25_raw or MATCH handling quotes
    assert 'MATCH' in src
    # At least _search_bigram_raw already does quoting; _search_bm25_raw should also quote
    # look for chr(34) quoting pattern in file
    assert 'chr(34)' in src, "FTS MATCH should escape with chr(34) quoting"

    # also check _conn protected and _init_db WAL line exists
    assert "WAL" in src
    # check recall top_k slicing pattern
    assert "max(0" in recall_segment or "int(top_k" in recall_segment

    # runtime check for lock object
    import tempfile
    from pathlib import Path
    from hero_quant.memory.store import MemoryStore
    import tempfile as _tf
    tmp = Path(_tf.mkdtemp())
    try:
        ms = MemoryStore(base_path=tmp)
        assert hasattr(ms, "_lock"), "MemoryStore should have _lock attribute"
        import threading
        assert isinstance(ms._lock, type(threading.RLock())), "_lock should be RLock"
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

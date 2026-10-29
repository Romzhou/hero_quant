"""MemoryStore 原子写与线程安全 —— 运行时行为断言（不再扫描源码文本）。"""

import sqlite3
import threading


def test_write_atomic_failure_cleans_file(tmp_path):
    from hero_quant.memory.store import MemoryStore

    ms = MemoryStore(base_path=tmp_path)
    ms.write("k", "content")
    assert (tmp_path / "k.md").exists(), "k.md should exist after write"
    # WAL 是运行时可观测状态，直接查 PRAGMA
    cur = ms._conn.cursor()
    cur.execute("PRAGMA journal_mode")
    assert str(cur.fetchone()[0]).lower() == "wal", "journal_mode 必须是 WAL"

    # --- atomic double-write failure should clean orphan file ---
    original_commit = ms._conn.commit

    def failing_commit():
        raise sqlite3.OperationalError("simulated DB failure")

    ms._conn.commit = failing_commit
    try:
        ms.write("k2", "second content")
    except Exception:
        pass
    finally:
        ms._conn.commit = original_commit
    assert not (tmp_path / "k2.md").exists(), "orphan k2.md should be cleaned on DB failure (atomic double-write)"


def test_recent_hashes_locked(tmp_path):
    """并发写/搜不得丢内容，且锁对象是 RLock；recall 的 top_k 必须生效。"""
    from hero_quant.memory.store import MemoryStore

    ms = MemoryStore(base_path=tmp_path)
    assert isinstance(ms._lock, type(threading.RLock())), "_lock should be RLock"

    errors = []
    barrier = threading.Barrier(8)

    def worker(tid):
        try:
            barrier.wait(timeout=5)
            for i in range(15):
                ms.write(f"t{tid}_{i}", f"content {tid} {i} keyword_{tid}")
                ms.search(f"keyword_{tid}")
        except Exception as exc:  # 收集后统一断言，避免线程里直接失败丢栈
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not errors, f"并发写/搜抛错: {errors!r}"
    for tid in range(8):
        for i in range(15):
            assert (tmp_path / f"t{tid}_{i}.md").exists(), f"并发下丢失 t{tid}_{i}.md"

    results = ms.recall("keyword_3", top_k=3)
    assert len(results) <= 3, f"recall 未按 top_k 截断: {len(results)}"

# tests/test_memory.py
def test_memory_write_and_search(tmp_path):
    from hero_quant.memory.store import MemoryStore
    ms = MemoryStore(tmp_path)
    ms.write("note1", "贵州茅台 600519 财报超预期")
    ms.write("note1", "贵州茅台 600519 财报超预期") # 30s内去重
    assert len(ms.search("茅台")) == 1
    try:
        ms.close()
    except Exception:
        pass


def test_memory_write_validation_rejects_empty(tmp_path):
    """P2: write must reject empty key/content with ValueError and log warning."""
    from hero_quant.memory.store import MemoryStore
    import pytest
    ms = MemoryStore(tmp_path)
    with pytest.raises(ValueError):
        ms.write("", "valid content")
    with pytest.raises(ValueError):
        ms.write("valid_key", "")
    with pytest.raises(ValueError):
        ms.write("   ", "content")
    with pytest.raises(ValueError):
        ms.write("k", "   ")
    try:
        ms.close()
    except Exception:
        pass


def test_recent_hashes_bounded(tmp_path):
    """P2: _recent_hashes and _meta must be bounded to avoid unbounded growth."""
    from hero_quant.memory.store import MemoryStore
    ms = MemoryStore(tmp_path)
    # Write many distinct contents rapidly within 30s window
    for i in range(3000):
        ms.write(f"k{i}", f"content uniq {i} {i*7}")
    # After fix should be capped (2048 for recent_hashes, 4096 for meta)
    assert len(ms._recent_hashes) <= 2500, f"_recent_hashes unbounded {len(ms._recent_hashes)}"
    assert len(ms._meta) <= 5000, f"_meta unbounded {len(ms._meta)}"
    try:
        ms.close()
    except Exception:
        pass


def test_search_cache_deepcopy_isolation(tmp_path):
    """P2: shallow copies leaking state - retrieval cache must return deep copy."""
    from hero_quant.memory.store import MemoryStore
    ms = MemoryStore(tmp_path)
    ms.write("k1", "cache isolation test content alpha")
    r1 = ms.search("cache isolation")
    assert len(r1) >= 1
    # mutate returned dict
    r1[0]["content"] = "MUTATED"
    r2 = ms.search("cache isolation")
    assert r2[0]["content"] != "MUTATED", "cache shallow copy leak"
    try:
        ms.close()
    except Exception:
        pass


def test_memory_store_close_idempotent(tmp_path):
    """P2: MemoryStore must expose close() and be idempotent, avoid handle leak on Windows."""
    from hero_quant.memory.store import MemoryStore
    ms = MemoryStore(tmp_path)
    ms.write("kclose", "close test")
    ms.close()
    ms.close()  # second close should not raise
    # after close, new instance can still open same path
    ms2 = MemoryStore(tmp_path)
    assert len(ms2.search("close test")) >= 1
    ms2.close()


def test_content_hash_cross_key_dedup(tmp_path):
    """同 key 同内容 30s 内判重；跨 key 同内容保留（distinct keys must persist）。"""
    from hero_quant.memory.store import MemoryStore

    # 同 key 同内容：30s 内判重，DB 仅 1 条
    ms = MemoryStore(tmp_path)
    ms.write("a", "hello")
    ms.write("a", "hello")
    cur = ms._conn.cursor()
    cur.execute("SELECT COUNT(*) FROM notes")
    assert cur.fetchone()[0] == 1

    # 跨 key 同内容：distinct keys must persist，各保留一条（不因内容相同而丢失）
    ms2 = MemoryStore(tmp_path / "cross_key")
    ms2.write("a", "hello")
    ms2.write("b", "hello")
    cur2 = ms2._conn.cursor()
    cur2.execute("SELECT COUNT(*) FROM notes")
    assert cur2.fetchone()[0] == 2

    # 大小写/空白归一：同 key 标准化后仍判重
    ms3 = MemoryStore(tmp_path / "case_ws")
    ms3.write("a", "hello")
    ms3.write("a", "  HELLO  ")
    cur3 = ms3._conn.cursor()
    cur3.execute("SELECT COUNT(*) FROM notes")
    assert cur3.fetchone()[0] == 1

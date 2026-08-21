# tests/test_memory.py
def test_memory_write_and_search(tmp_path):
    from hero_quant.memory.store import MemoryStore
    ms = MemoryStore(tmp_path)
    ms.write("note1", "贵州茅台 600519 财报超预期")
    ms.write("note1", "贵州茅台 600519 财报超预期") # 30s内去重
    assert len(ms.search("茅台")) == 1


def test_content_hash_cross_key_dedup(tmp_path):
    """A3-1 TDD: 同一内容不同 key 30s 内第二笔应被判重，大小写/空白归一."""
    from hero_quant.memory.store import MemoryStore

    ms = MemoryStore(tmp_path)
    ms.write("a", "hello")
    ms.write("b", "hello")  # 同内容不同 key，30s 内判重
    assert len(ms.search("hello")) == 1
    # DB 层面也应只有 1 条，避免 search 去重掩盖
    cur = ms._conn.cursor()
    cur.execute("SELECT COUNT(*) FROM notes")
    assert cur.fetchone()[0] == 1

    # 大小写/空白归一：标准化后仍判重
    ms2 = MemoryStore(tmp_path / "case_ws")
    ms2.write("a", "hello")
    ms2.write("b", "  HELLO  ")
    assert len(ms2.search("hello")) == 1
    cur2 = ms2._conn.cursor()
    cur2.execute("SELECT COUNT(*) FROM notes")
    assert cur2.fetchone()[0] == 1

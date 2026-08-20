# tests/test_memory.py
def test_memory_write_and_search(tmp_path):
    from hero_quant.memory.store import MemoryStore
    ms = MemoryStore(tmp_path)
    ms.write("note1", "贵州茅台 600519 财报超预期")
    ms.write("note1", "贵州茅台 600519 财报超预期") # 30s内去重
    assert len(ms.search("茅台")) == 1

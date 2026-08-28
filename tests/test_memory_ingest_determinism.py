"""Task24 ingest deterministic key + chunk validation."""
import hashlib


def test_ingest_key_deterministic(tmp_path):
    from hero_quant.memory.ingest import ingest_markdown

    p = tmp_path / "a" / "b" / "report.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# hi\nhello", encoding="utf-8")

    class FakeStore:
        def __init__(self):
            self.keys = []
            self.contents = []

        def write(self, key, content):
            self.keys.append(key)
            self.contents.append(content)

    store1 = FakeStore()
    store2 = FakeStore()
    # Use same store type but fresh instances to test determinism
    k1 = ingest_markdown(str(p), store=store1)
    k2 = ingest_markdown(str(p), store=store2)
    assert k1 == k2
    assert store1.keys == store2.keys, "keys should be deterministic across runs"
    # key should be full path namespace + 16 hex
    assert len(store1.keys) > 0
    for k in store1.keys:
        # last segment after : should be 16 hex
        hexpart = k.split(":")[-1]
        assert len(hexpart) == 16, f"expected 16 hex got {hexpart!r} in {k!r}"
        int(hexpart, 16)
        # namespace should contain resolved path
        assert p.resolve().as_posix() in k
        # no idx dependency: re-ingest with same content yields same key without idx shift
        assert hashlib.sha256(store1.contents[0].encode()).hexdigest()[:16] in k


def test_chunk_overlap_validation():
    from hero_quant.memory.ingest import _overlap_chunks
    import pytest

    with pytest.raises(ValueError):
        _overlap_chunks("hi", chunk=0, overlap=0)
    with pytest.raises(ValueError):
        _overlap_chunks("hi", chunk=10, overlap=10)
    with pytest.raises(ValueError):
        _overlap_chunks("hi", chunk=10, overlap=11)
    with pytest.raises(ValueError):
        _overlap_chunks("hi", chunk=-1, overlap=0)

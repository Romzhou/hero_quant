import sqlite3


def test_two_character_cjk_query_uses_content_bigram_index(tmp_path):
    from hero_quant.memory.store import MemoryStore

    ms = MemoryStore(tmp_path)
    ms.write("maotai", "贵州茅台 600519 财报超预期")
    ms._vector_enabled = False

    assert ms._bigram_enabled is True
    row = ms._conn.execute(
        "SELECT bigrams FROM notes_fts_bigram WHERE rowid = (SELECT id FROM notes WHERE key = ?)",
        ("maotai",),
    ).fetchone()
    assert row is not None
    assert "茅台" in row[0].split()

    results = ms.search("茅台")
    assert any(result["key"] == "maotai" for result in results)


def test_trigram_build_failure_falls_back_to_bigram(monkeypatch, tmp_path):
    from hero_quant.memory import store as store_module

    real_connect = store_module.sqlite3.connect

    class ConnectionProxy:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, *args, **kwargs):
            if "tokenize='trigram'" in sql:
                raise sqlite3.OperationalError("trigram unavailable")
            return self._connection.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    def connect(*args, **kwargs):
        return ConnectionProxy(real_connect(*args, **kwargs))

    monkeypatch.setattr(store_module.sqlite3, "connect", connect)

    ms = store_module.MemoryStore(tmp_path)
    assert ms._trigram_enabled is False
    assert ms._bigram_enabled is True

    ms.write("cn", "量化策略需要中文二字检索")
    ms._vector_enabled = False
    results = ms.search("中文")
    assert any(result["key"] == "cn" for result in results)


def test_external_index_searches_existing_summary_without_writing_base_file(tmp_path):
    from hero_quant.memory.store import MemoryStore

    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    source = daily_dir / "2026-08-22.md"
    source.write_text("外部摘要包含中文检索 alpha signal", encoding="utf-8")

    ms = MemoryStore(tmp_path)
    ms._vector_enabled = False
    key = "daily/2026-08-22.md"

    ms.index_external(key, source.read_text(encoding="utf-8"))

    assert not (tmp_path / "daily__2026-08-22.md.md").exists()
    assert any(result["key"] == key for result in ms.search("中文"))
    assert any(result["key"] == key for result in ms.search("alpha"))


def test_external_index_is_idempotent_and_updates_same_key(tmp_path):
    from hero_quant.memory.store import MemoryStore

    ms = MemoryStore(tmp_path)
    ms._vector_enabled = False
    key = "digest/2026-08.md"

    ms.index_external(key, "摘要包含中文 alpha")
    ms.index_external(key, "摘要更新包含中文 beta")

    row = ms._conn.execute(
        "SELECT COUNT(*), content FROM notes WHERE key = ?", (key,)
    ).fetchone()
    assert row == (1, "摘要更新包含中文 beta")
    assert any(result["key"] == key for result in ms.search("beta"))
    assert not any(result["key"] == key for result in ms.search("alpha"))

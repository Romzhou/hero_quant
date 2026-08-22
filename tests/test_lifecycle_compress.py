"""W2-H lifecycle GC and lightweight compression pipeline tests."""

import os
import time
from pathlib import Path


class _Memory:
    def __init__(self, base: Path) -> None:
        self.base = base
        self._meta: dict[str, dict] = {}


class _IndexingMemory(_Memory):
    def __init__(self, base: Path, fail_index: bool = False) -> None:
        super().__init__(base)
        self.indexed: list[tuple[str, str]] = []
        self.fail_index = fail_index

    def index_external(self, key: str, content: str) -> None:
        if self.fail_index:
            raise RuntimeError("index unavailable")
        self.indexed.append((key, content))

    def search(self, query: str) -> list[dict]:
        return [
            {"key": key, "content": content}
            for key, content in self.indexed
            if query.lower() in content.lower()
        ]


def _write_record(base: Path, name: str, content: str | bytes, age_days: float) -> Path:
    path = base / f"{name}.md"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    timestamp = time.time() - age_days * 86400
    os.utime(path, (timestamp, timestamp))
    return path


def test_delete_requires_max_age_when_enabled(tmp_path):
    """Delete is reserved for very old records; younger low scores still archive."""
    from hero_quant.memory.lifecycle import MAX_AGE, MemoryLifecycle

    assert MAX_AGE == 30
    memory = _Memory(tmp_path)
    mid_path = _write_record(tmp_path, "mid_low", "stale but recoverable", 14)
    old_path = _write_record(tmp_path, "very_old", "stale and disposable", MAX_AGE)
    memory._meta.update(
        {
            "mid_low": {"quality_score": 0.01, "access_count": 0, "last_accessed": time.time() - 14 * 86400},
            "very_old": {"quality_score": 0.01, "access_count": 0, "last_accessed": time.time() - MAX_AGE * 86400},
        }
    )

    lifecycle = MemoryLifecycle(memory)
    assert MemoryLifecycle.ENABLE_DELETE is True
    assert lifecycle.ENABLE_DELETE is True
    actions = lifecycle.run_gc(dry_run=False)

    by_name = {action["name"]: action["action"] for action in actions}
    assert by_name["mid_low"] == "archive"
    assert by_name["very_old"] == "delete"
    assert not mid_path.exists()
    assert not old_path.exists()
    assert (tmp_path / "archive" / mid_path.name).exists()


def test_compress_raw_records_to_daily(tmp_path):
    """Raw records at least seven days old are summarized into a daily file."""
    from hero_quant.memory.lifecycle import MemoryLifecycle

    _write_record(tmp_path, "raw_a", "alpha market trend. alpha signal remains useful.", 8)
    _write_record(tmp_path, "raw_b", "beta market trend. beta signal needs review.", 8)

    lifecycle = MemoryLifecycle(_Memory(tmp_path))
    actions = lifecycle.compress(dry_run=False)

    assert len(actions) == 1
    assert actions[0]["action"] == "compress"
    assert actions[0]["stage"] == "daily"
    daily_files = list((tmp_path / "daily").glob("*.md"))
    assert len(daily_files) == 1
    daily_text = daily_files[0].read_text(encoding="utf-8")
    assert "alpha" in daily_text
    assert "beta" in daily_text
    assert not (tmp_path / "raw_a.md").exists()
    assert not (tmp_path / "raw_b.md").exists()


def test_compress_daily_records_to_digest(tmp_path):
    """Daily records at least thirty days old are summarized into a digest file."""
    from hero_quant.memory.lifecycle import MAX_AGE, MemoryLifecycle

    daily = tmp_path / "daily"
    daily.mkdir()
    daily_path = _write_record(daily, "2026-07-01", "daily alpha finding. daily beta finding.", MAX_AGE + 1)

    lifecycle = MemoryLifecycle(_Memory(tmp_path))
    actions = lifecycle.compress(dry_run=False)

    assert len(actions) == 1
    assert actions[0]["action"] == "compress"
    assert actions[0]["stage"] == "digest"
    digest_files = list((tmp_path / "digest").glob("*.md"))
    assert len(digest_files) == 1
    digest_text = digest_files[0].read_text(encoding="utf-8")
    assert "alpha" in digest_text
    assert "beta" in digest_text
    assert not daily_path.exists()


def test_compress_ignores_empty_and_corrupt_records(tmp_path):
    """Malformed input must not abort compression or become summary content."""
    from hero_quant.memory.lifecycle import MemoryLifecycle

    _write_record(tmp_path, "empty", "", 8)
    _write_record(tmp_path, "corrupt", b"\xff\xfe\x00", 8)
    _write_record(tmp_path, "valid", "valid market observation.", 8)

    lifecycle = MemoryLifecycle(_Memory(tmp_path))
    actions = lifecycle.compress(dry_run=False)

    assert len(actions) == 1
    daily_files = list((tmp_path / "daily").glob("*.md"))
    assert len(daily_files) == 1
    daily_text = daily_files[0].read_text(encoding="utf-8")
    assert "valid market observation" in daily_text
    assert "corrupt" not in daily_text
    assert (tmp_path / "empty.md").exists()
    assert (tmp_path / "corrupt.md").exists()


def test_compress_makes_daily_summary_searchable_in_external_index(tmp_path):
    """A written daily summary is visible through the optional external index."""
    from hero_quant.memory.lifecycle import MemoryLifecycle

    memory = _IndexingMemory(tmp_path)
    _write_record(tmp_path, "raw_a", "daily searchable alpha finding.", 8)

    actions = MemoryLifecycle(memory).compress(dry_run=False)

    assert len(memory.indexed) == 1
    key, content = memory.indexed[0]
    assert key == f"compression:daily:{actions[0]['name']}"
    assert "daily searchable alpha finding" in content
    assert memory.search("searchable") == [{"key": key, "content": content}]


def test_compress_makes_digest_searchable_in_external_index(tmp_path):
    """A written digest is visible through the optional external index."""
    from hero_quant.memory.lifecycle import MAX_AGE, MemoryLifecycle

    daily = tmp_path / "daily"
    daily.mkdir()
    _write_record(daily, "2026-07-01", "digest searchable beta finding.", MAX_AGE + 1)
    memory = _IndexingMemory(tmp_path)

    actions = MemoryLifecycle(memory).compress(dry_run=False)

    assert len(memory.indexed) == 1
    key, content = memory.indexed[0]
    assert key == f"compression:digest:{actions[0]['name']}"
    assert "digest searchable beta finding" in content
    assert memory.search("searchable") == [{"key": key, "content": content}]


def test_compress_does_not_index_archived_sources_twice(tmp_path):
    """Re-running compression after source archival does not duplicate indexing."""
    from hero_quant.memory.lifecycle import MemoryLifecycle

    memory = _IndexingMemory(tmp_path)
    _write_record(tmp_path, "raw_once", "index exactly once.", 8)
    lifecycle = MemoryLifecycle(memory)

    lifecycle.compress(dry_run=False)
    lifecycle.compress(dry_run=False)

    assert len(memory.indexed) == 1


def test_external_index_failure_does_not_block_compression_file_pipeline(tmp_path):
    """An external index failure leaves the summary file and source archival intact."""
    from hero_quant.memory.lifecycle import MemoryLifecycle

    memory = _IndexingMemory(tmp_path, fail_index=True)
    source = _write_record(tmp_path, "raw_failure", "file pipeline survives.", 8)

    actions = MemoryLifecycle(memory).compress(dry_run=False)

    assert len(actions) == 1
    target = Path(actions[0]["target"])
    assert target.exists()
    assert not source.exists()

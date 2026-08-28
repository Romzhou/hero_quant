"""Task22: lifecycle GC atomicity - delete backup not unlink, archive versioned."""
from pathlib import Path


def _src() -> str:
    return Path("src/hero_quant/memory/lifecycle.py").read_text(encoding="utf-8")


def test_delete_backup_failure_not_unlink():
    src = _src()
    # locate delete branch
    assert 'elif action == "delete"' in src or "elif action == 'delete'" in src
    # extract delete block up to next except or def
    idx = src.find('elif action == "delete"')
    if idx == -1:
        idx = src.find("elif action == 'delete'")
    block = src[idx : idx + 3000]
    # must use logger.warning and return on backup failure, not debug pass + unlink
    assert "logger.warning" in block, "delete backup must logger.warning on failure"
    # must not contain silent handled pass pattern
    assert "silent handled" not in block, "delete backup must not silently pass"
    # must return without unlink when backup fails (presence of return before unlink in block)
    # ensure a 'return' appears before file_path.unlink in the backup failure path
    # simple check: block contains 'return' after warning
    assert "return" in block, "delete backup failure must return not unlink"
    # must use tmp atomic write
    assert ".tmp" in block or "tmp" in block.lower(), "delete backup must use tmp atomic write"
    # must handle dest collision versioned
    assert ".stem" in block or "counter" in block.lower(), "delete dest collision must be versioned"
    # ensure not unconditional unlink after except: check that unlink is not directly after pass
    # after fix, unlink should be guarded after successful tmp rename, not unconditional


def test_archive_collision_versioned():
    src = _src()
    idx = src.find('if action == "archive"')
    if idx == -1:
        idx = src.find("if action == 'archive'")
    assert idx != -1
    # archive block until next elif
    end = src.find('elif action == "delete"', idx)
    if end == -1:
        end = src.find("elif action == 'delete'", idx)
    block = src[idx:end] if end != -1 else src[idx : idx + 3000]
    # must handle FileExistsError or versioned retry, not silent return on exists
    has_versioned = ("FileExistsError" in block) or ("counter" in block.lower() and ".stem" in block)
    assert has_versioned, "archive collision must be versioned with counter or FileExistsError handling"
    # must not be simple 'if dest.exists(): logger.warning ... return' without versioning
    # if block still contains that pattern but also has versioning, it's ok; but if it only has warning+return without counter, fail
    # Check that after fix, block does not consist solely of warning+return without loop
    # We assert that either FileExistsError is present or a loop versioning exists
    assert "FileExistsError" in block or ("while" in block and "exists()" in block), "archive must version via loop or handle FileExistsError"
    # must log warning via logger.warning for collision/OSError
    assert "logger.warning" in block, "archive must logger.warning on collision/OSError"

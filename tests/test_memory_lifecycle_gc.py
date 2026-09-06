"""Task22: lifecycle GC atomicity - delete backup not unlink, archive versioned."""
from pathlib import Path


def _src() -> str:
    return Path("src/hero_quant/memory/lifecycle.py").read_text(encoding="utf-8")


def test_delete_backup_failure_not_unlink():
    """delete 即 purge：直接 unlink（非备份），unlink 失败时 logger.warning，不静默 pass。

    中文：lane F1 将 delete 语义定为「purge（直接 unlink），保留走显式 archive」，
    而非「先备份 tmp 再删」。故删除对 .tmp/.stem 备份的断言，改为校验 purge 语义。
    """
    src = _src()
    assert 'elif action == "delete"' in src or "elif action == 'delete'" in src
    idx = src.find('elif action == "delete"')
    if idx == -1:
        idx = src.find("elif action == 'delete'")
    block = src[idx : idx + 2000]
    # delete 直接 unlink（purge），不做 tmp 备份
    assert "unlink" in block, "delete 应直接 unlink（purge）"
    assert "logger.warning" in block, "delete unlink 失败须 logger.warning"
    assert "silent handled" not in block, "delete 不得静默 pass"


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

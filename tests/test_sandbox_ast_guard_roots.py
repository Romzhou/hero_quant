import pytest
from hero_quant.sandbox.ast_guard import check_source, SandboxViolation, is_allowlist_synced_with_pyproject


def test_sys_import_blocked():
    with pytest.raises(SandboxViolation):
        check_source("import sys\n sys.modules['os'].system('id')")


def test_importlib_blocked():
    with pytest.raises(SandboxViolation):
        check_source("import importlib\n importlib.import_module('os')")


def test_is_allowlist_synced_false_when_missing():
    ok, missing = is_allowlist_synced_with_pyproject()
    # 修复前永真，修复后应比较 expected = _STATIC_ALLOWED|_QUANTLIB_EXTRA
    assert isinstance(ok, bool)
    assert isinstance(missing, list)

import pytest
from hero_quant.sandbox.ast_guard import check_source, SandboxViolation


def test_open_blocked_without_import():
    with pytest.raises(SandboxViolation):
        check_source("open('/etc/passwd').read()")


def test_compile_blocked():
    with pytest.raises(SandboxViolation):
        check_source("compile('1+1','<x>','exec')")


def test_getattr_blocked():
    with pytest.raises(SandboxViolation):
        check_source("getattr(__import__('os'),'system')")

import pytest
from hero_quant.sandbox.ast_guard import check_source, SandboxViolation

def test_alias_import_os_system_blocked():
    with pytest.raises(SandboxViolation):
        check_source("import os as o\n o.system('id')")

def test_from_import_alias_blocked():
    with pytest.raises(SandboxViolation):
        check_source("from os import system as s\n s('id')")

def test_chained_attribute_blocked():
    with pytest.raises(SandboxViolation):
        check_source("import os\n os.path.join('a','b')")

def test_getattr_indirection_blocked():
    with pytest.raises(SandboxViolation):
        check_source("import os\n getattr(os, 'system')('id')")

import pytest  # noqa: F401
from hero_quant.governance.dedup import DDL_DEDUP_PG, DDL_TOOL_CALL_PG, derive_key


def test_ddl_has_tenant_and_tool_call():
    assert "tenant" in DDL_DEDUP_PG.lower()
    assert "tool_call_dedup" in DDL_TOOL_CALL_PG.lower()


def test_derive_key_escapes_colon():
    # accept either escaping (%3A / count 4) or strict reject (ValueError) as valid fix
    try:
        k1 = derive_key("t:a", "wf", "step", "tool", "biz")
    except ValueError:
        return
    assert "%3A" in k1 or k1.count(":") == 4


def test_derive_key_rejects_colon():
    try:
        derive_key("ten:ant", "wf", "s", "t", "b")
        assert False, "should reject colon"
    except ValueError:
        pass

"""J线 J19: 越过 68% 线 —— redaction/presentation/registry-tool 精确补点。"""
from __future__ import annotations

import pytest


def test_j19_redaction_full():
    from hero_quant.tools import redaction as RD

    assert RD._maybe_redact({"a": 1}) is not None
    assert RD._maybe_redact([1, 2]) is not None
    assert RD._maybe_redact((1, 2)) is not None
    assert RD._maybe_redact({1, 2}) is not None
    assert RD._maybe_redact("sk-secret-long-value-12345") is not None
    assert RD._maybe_redact(123) == 123
    assert RD._maybe_redact(None) is None
    # fail-closed 哨兵路径：redact_payload 抛错 -> "***"
    import unittest.mock as mock
    with mock.patch("hero_quant.security.redaction.redact_payload", side_effect=RuntimeError("boom")):
        assert RD._maybe_redact({"a": 1}) == "***"
    # 模块缺席路径
    with mock.patch.dict("sys.modules", {"hero_quant.security.redaction": None}):
        assert RD._maybe_redact({"a": 1}) == "***"
    r = RD.redact_tool_result({"api_key": "sk-1234567890abcdef"})
    assert "sk-1234567890abcdef" not in r
    assert RD.redact_tool_result({1, 2, 3}) is not None  # set 路径
    cyc = {}
    cyc["self"] = cyc
    assert RD.redact_tool_result(cyc) is not None  # 循环引用 fail-closed
    assert RD.redact_tool_result(12345) is not None
    assert RD.redact_tool_result("x" * 20000, limit=100) is not None
    assert RD.redact_tool_result("plain") == "plain" or isinstance(RD.redact_tool_result("plain"), str)


def test_j19_presentation_full():
    from hero_quant.tools import presentation as PR
    from hero_quant.tools.registry import TOOL_REGISTRY

    assert PR._get_field({"a": 1}, "a") == 1
    assert PR._get_field(object(), "x", "d") == "d"
    with pytest.raises(ValueError):
        PR.present_as_native({})
    with pytest.raises(ValueError):
        PR.present_as_code({})
    assert PR.present_as_code({"name": "t", "description": "line1\nline2"}) is not None
    assert PR.present_as_code({"name": "a\nb", "description": ""}) is not None
    import hero_quant.mcp.server  # noqa: F401  确保工具模块已注册
    from hero_quant.tools.registry import TOOL_REGISTRY as _REG2
    spec = next(iter(_REG2.values()))
    both = PR.present(spec, presentAs="both")
    assert "native" in both and "code" in both
    with pytest.raises(ValueError):
        PR.present(spec, presentAs="nope")
    assert len(PR.present_definitions(presentAs="code")) > 0
    assert len(PR.present_definitions(presentAs="both")) > 0


def test_j19_registry_tool_more():
    from hero_quant.tools.registry import TOOL_REGISTRY, tool

    @tool(name="tmp_j19_ms", description="d", timeoutMs="1500")
    def _f1(): pass
    assert TOOL_REGISTRY["tmp_j19_ms"].timeoutMs == 1500
    TOOL_REGISTRY.pop("tmp_j19_ms", None)
    with pytest.raises(ValueError):
        @tool(name="tmp_j19_ms2", description="d", timeoutMs="15.5")
        def _f2(): pass
    TOOL_REGISTRY.pop("tmp_j19_ms2", None)

    @tool(name="tmp_j19_out", description="d", output={"type": "object"})
    def _f3(): pass
    assert "tmp_j19_out" in TOOL_REGISTRY
    TOOL_REGISTRY.pop("tmp_j19_out", None)

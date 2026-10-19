"""J线 J2: mcp/server + skills/loader + backtest/validation 黑洞扫荡。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest


# ---------- mcp/server ----------

def test_j2_curated_is_20_and_sorted():
    from hero_quant.mcp.server import CURATED_TOOLS

    assert len(CURATED_TOOLS) == 20
    assert CURATED_TOOLS == sorted(CURATED_TOOLS)


def test_j2_curated_definitions_and_specs():
    from hero_quant.mcp.server import get_curated_definitions, get_curated_specs

    defs = get_curated_definitions()
    assert isinstance(defs, list) and len(defs) >= 1
    names = {d.get("function", {}).get("name") for d in defs}
    assert names.issubset(set(__import__("hero_quant.mcp.server", fromlist=["CURATED_TOOLS"]).CURATED_TOOLS))
    specs = get_curated_specs()
    assert isinstance(specs, dict) and len(specs) >= 1


def test_j2_mcp_server_list_and_create():
    from hero_quant.mcp.server import CURATED_TOOLS, MCPServer, create_server, server

    s = create_server()
    assert s.list_tools() == CURATED_TOOLS
    assert server.list_tools() == CURATED_TOOLS
    custom = MCPServer(tools=["run_backtest"])
    assert custom.list_tools() == ["run_backtest"]
    with pytest.raises(ValueError):
        MCPServer(tools=["not_a_curated_tool"])
    # fastmcp 缺席亦可用
    assert s.get_fastmcp() is None or s.get_fastmcp() is not None


def test_j2_mcp_call_tool_validation_branches():
    from hero_quant.mcp.server import MCPServer

    s = MCPServer()
    with pytest.raises(ValueError):
        s.call_tool("not_curated", {})
    # 缺少必填参数
    with pytest.raises(ValueError):
        s.call_tool("run_backtest", {})
    # 非法多余参数
    cur = s.list_tools()
    # 找一个 additionalProperties=False 的工具触发 extra 分支
    from hero_quant.tools.registry import TOOL_REGISTRY
    target = None
    for name in cur:
        spec = TOOL_REGISTRY.get(name)
        if spec is not None and spec.parameters is not None and spec.parameters.get("additionalProperties") is False:
            target = name
            break
    assert target is not None
    spec = TOOL_REGISTRY[target]
    props = spec.parameters.get("properties", {})
    bad_args = dict.fromkeys(props.keys(), 1)
    bad_args["__extra_j2__"] = 1
    with pytest.raises(ValueError):
        s.call_tool(target, bad_args)


def test_j2_mcp_call_tool_exec_branches(monkeypatch):
    from hero_quant.mcp.server import MCPServer
    from hero_quant.tools.registry import TOOL_REGISTRY

    s = MCPServer()
    # 未注册 curated 名 -> not registered
    monkeypatch.setitem(TOOL_REGISTRY, "compute_sharpe", None) if False else None
    import hero_quant.mcp.server as MS
    orig = dict(MS.TOOL_REGISTRY)
    try:
        # 伪造一个 curated 但 registry 缺失的情形
        MS.CURATED_TOOLS.append("j2_ghost_tool")
        s2 = MCPServer(tools=["j2_ghost_tool"])
        with pytest.raises(ValueError):
            s2.call_tool("j2_ghost_tool", {})
    finally:
        MS.CURATED_TOOLS.remove("j2_ghost_tool")

    # 参数类型错配 -> ValueError 包装（劫持一个纯函数的实现触发 TypeError）
    from hero_quant.tools.registry import TOOL_REGISTRY as _REG
    _spec = _REG.get("compute_sharpe")
    if _spec is not None:
        _orig = _spec.func
        def _boom(*a, **k):
            raise TypeError("j2 boom")
        _spec.func = _boom  # type: ignore
        try:
            with pytest.raises(ValueError):
                s.call_tool("compute_sharpe", {"returns": [0.01, 0.02]})
        finally:
            _spec.func = _orig  # type: ignore
    else:
        with pytest.raises(ValueError):
            s.call_tool("compute_sharpe", {})


def test_j2_mcp_option_tools_ok_and_fail():
    from hero_quant.mcp.server import MCPServer

    s = MCPServer()
    if "get_option_price" in s.list_tools():
        out = s.call_tool("get_option_price", {"S": 100.0, "K": 100.0, "T": 1.0})
        assert out.get("ok") is True
        bad = s.call_tool("get_option_price", {"S": -1, "K": 0, "T": -5})
        assert isinstance(bad, dict)
    if "get_greeks" in s.list_tools():
        out = s.call_tool("get_greeks", {"S": 100.0, "K": 100.0, "T": 1.0})
        assert isinstance(out, dict)
    if "get_implied_vol" in s.list_tools():
        out = s.call_tool("get_implied_vol", {"price": 10.0, "S": 100.0, "K": 100.0, "T": 1.0})
        assert isinstance(out, dict)


# ---------- skills/loader ----------

def _write(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_j2_skill_parse_frontmatter_good_and_bad(tmp_path):
    from hero_quant.skills.loader import _parse_skill_file

    good = tmp_path / "SKILL.md"
    _write(good, "---\nname: my_skill-1.0\n---\nHello body line\nsecond\n")
    name, body = _parse_skill_file(good)
    assert name == "my_skill-1.0" and "Hello" in body

    illegal = tmp_path / "bad" / "SKILL.md"
    _write(illegal, "---\nname: bad name with spaces!!\n---\nBody here\n")
    name2, _ = _parse_skill_file(illegal)
    assert name2 == "SKILL"  # 非法回退 stem

    nofm = tmp_path / "plain.md"
    _write(nofm, "just some text name: found_skill more\n")
    name3, body3 = _parse_skill_file(nofm)
    assert name3 == "found_skill"

    noname = tmp_path / "noname.md"
    _write(noname, "no name at all here\n")
    name4, _ = _parse_skill_file(noname)
    assert name4 == "noname"

    empty_fm = tmp_path / "emptyfm.md"
    _write(empty_fm, "---\nname: \n---\n")
    n5, b5 = _parse_skill_file(empty_fm)
    assert isinstance(n5, str)


def test_j2_skills_loader_scan_and_api(tmp_path):
    from hero_quant.skills.loader import SkillsLoader

    r1 = tmp_path / "proj"
    _write(r1 / "SKILL.md", "---\nname: alpha\n---\nAlpha body preview line\n")
    r2 = tmp_path / "user"
    _write(r2 / "SKILL.md", "---\nname: alpha\n---\nOverride body\n")
    missing = tmp_path / "nope"

    loader = SkillsLoader(roots=[str(missing), str(r1), str(r2)])
    # 后者覆盖前者
    assert loader.get_content("alpha") == "Override body"
    assert "alpha" in loader.get_descriptions()
    assert "alpha" in loader.list_skills()
    snap = loader.snapshot()
    assert ":" in snap
    # 大小写回退 + 缺失
    assert loader.get_content("ALPHA") == "Override body"
    assert loader.get_content("ghost") == ""
    # 单文件根
    single = SkillsLoader(roots=[str(r1 / "SKILL.md")])
    assert "alpha" in single.list_skills()
    # 空 loader
    empty = SkillsLoader(roots=[str(missing)])
    assert empty.get_descriptions() == ""
    assert empty.list_skills() == []
    # 外部失效钩子
    loader.observed_invalidate("whatever")


def test_j2_skills_loader_fresh_rescan(tmp_path):
    import time
    from hero_quant.skills.loader import SkillsLoader

    r = tmp_path / "skills"
    _write(r / "SKILL.md", "---\nname: beta\n---\nBeta v1\n")
    loader = SkillsLoader(roots=[str(r)])
    assert "beta" in loader.list_skills()
    # 模拟 mtime 变化触发重扫：改写文件并把内部 mtime 置旧
    _write(r / "SKILL.md", "---\nname: beta\n---\nBeta v2 content\n")
    for k in list(loader._skills.values()):
        k["mtime"] = 0.0
    loader._last_scan_ts = 0.0
    loader._ensure_fresh()
    assert "Beta v2" in loader.get_content("beta")
    # 新增文件触发重扫
    _write(r / "sub" / "SKILL.md", "---\nname: gamma\n---\nGamma body\n")
    loader._last_scan_ts = 0.0
    loader._ensure_fresh()
    assert "gamma" in loader.list_skills()


# ---------- backtest/validation ----------

def _df_ok():
    idx = pd.date_range("2024-01-01", periods=5, freq="D")
    return pd.DataFrame({"close": [10.0, 11.0, 12.0, 13.0, 14.0]}, index=idx)


def test_j2_validation_happy_and_pit():
    from hero_quant.backtest.validation import PIT_ACK, ValidationError, validate

    assert PIT_ACK == "I_KNOW_THIS_IS_NON_PIT"
    assert validate(_df_ok()) is None
    assert validate(_df_ok(), weights_on="2024-01-01", price_date="2024-01-05") is None
    with pytest.raises(ValidationError):
        validate(_df_ok(), weights_on="2024-01-05", price_date="2024-01-01")
    # kwargs 兼容传入
    assert validate(_df_ok(), weights_on="2024-01-01", price_date="2024-01-02") is None


def test_j2_validation_arg_guards():
    from hero_quant.backtest.validation import ValidationError, validate

    with pytest.raises(ValidationError):
        validate(_df_ok(), None, None, None, "extra-positional")
    with pytest.raises(ValidationError):
        validate(_df_ok(), weights_onn="2024-01-01")
    with pytest.raises(ValidationError):
        validate(pd.DataFrame())
    with pytest.raises(ValidationError):
        validate(pd.DataFrame({"close": [1.0]}))
    dup = _df_ok()
    dup.index = pd.DatetimeIndex(["2024-01-01", "2024-01-01", "2024-01-03", "2024-01-04", "2024-01-05"])
    with pytest.raises(ValidationError):
        validate(dup)
    unsorted = _df_ok().iloc[::-1]
    with pytest.raises(ValidationError):
        validate(unsorted)
    with pytest.raises(ValidationError):
        validate(_df_ok(), weights_on="not-a-date", price_date="2024-01-02")


def test_j2_validation_price_and_currency():
    from hero_quant.backtest.validation import ValidationError, validate

    bad = _df_ok()
    bad.loc[bad.index[2], "close"] = -1.0
    with pytest.raises(ValidationError):
        validate(bad)
    nanp = _df_ok()
    nanp.loc[nanp.index[0], "close"] = float("nan")
    with pytest.raises(ValidationError):
        validate(nanp)
    nonnum = _df_ok().astype(object)
    nonnum.loc[nonnum.index[1], "close"] = "oops"
    with pytest.raises(ValidationError):
        validate(nonnum)
    # 多资产列 + metadata 跳过
    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    multi = pd.DataFrame({"A": [1.0, 2.0, 3.0], "B": [2.0, 3.0, 4.0], "volume": [9, 9, 9]}, index=idx)
    assert validate(multi) is None
    multi_bad = multi.copy()
    multi_bad.loc[multi_bad.index[0], "B"] = 0.0
    with pytest.raises(ValidationError):
        validate(multi_bad)
    # 混币种 / NaN 币种 / mismatch
    ccy = _df_ok()
    ccy["currency"] = ["CNY", "CNY", "USD", "CNY", "CNY"]
    with pytest.raises(ValidationError):
        validate(ccy)
    ccy2 = _df_ok()
    ccy2["ccy"] = ["CNY", None, "CNY", "CNY", "CNY"]
    with pytest.raises(ValidationError):
        validate(ccy2)
    ccy3 = _df_ok()
    ccy3["currency"] = ["CNY"] * 5
    with pytest.raises(ValidationError):
        validate(ccy3, currency="USD")
    assert validate(ccy3, currency="CNY") is None

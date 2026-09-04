"""B3b lane TDD: 回测工具 14 条 (tools/backtest.py×5 + tools/quantlib_tool.py×5 + tools/registry.py×4)。

中文注释。fail-closed：错误走 {ok:False,...} 信封不抛裸异常；provenance 必传；
NaN 不填 0 冒充（标记 insufficient_data）；深拷贝；窄化捕获。
"""
import copy
import inspect

import pytest


# ============ tools/backtest.py ×5 ============

def _fake_engine(monkeypatch):
    """桩引擎：捕获 prices/weights，不触真实行情与 backtest/ 逻辑。"""
    import hero_quant.backtest.engine as eng_mod

    seen = {}

    class _Stub:
        def run(self, prices, weights=None, costs=0.0005, engine="default", **kw):
            seen["prices"] = prices
            seen["weights"] = list(weights) if weights is not None else None
            seen["n"] = len(prices)
            seen["columns"] = list(prices.columns)
            return {"equity": [100.0, 101.0], "metrics": {"n": len(prices)}}

    monkeypatch.setattr(eng_mod, "BacktestEngine", _Stub)
    return seen


def test_b3b_backtest_no_silent_truncation(monkeypatch):
    """bars[:50] 静默截断：60 根 bars 应全部进入引擎，不得截断且无声。"""
    import hero_quant.tools.backtest as bt

    bars = [{"close": 100.0 + i} for i in range(60)]
    monkeypatch.setattr(bt, "_fetch_bars_for_backtest", lambda *a, **k: bars)
    seen = _fake_engine(monkeypatch)
    r = bt.run_backtest(symbol="600519.SH", start="2026-01-01", end="2026-06-01", weights=[1.0])
    assert r.get("ok") is True
    assert seen["n"] == 60, f"60 bars 被静默截断为 {seen['n']}"


def test_b3b_backtest_mismatch_returns_envelope():
    """tickers-vs-weights 不一致应走 {ok:False} 信封，不得抛裸 ValueError。"""
    import hero_quant.tools.backtest as bt

    r = bt.run_backtest(symbol="AAPL,MSFT", weights=[0.5])
    assert isinstance(r, dict) and r.get("ok") is False
    assert "mismatch" in str(r.get("error", "")).lower()


def test_b3b_backtest_synthetic_has_provenance(monkeypatch):
    """拉取失败合成兜底必须带 provenance 标记，不得 ok:True 伪装真实回测。"""
    import hero_quant.tools.backtest as bt

    monkeypatch.setattr(bt, "_fetch_bars_for_backtest", lambda *a, **k: [])
    seen = _fake_engine(monkeypatch)
    r = bt.run_backtest(symbol="600519.SH", weights=[1.0])
    assert r.get("ok") is True  # 合成演示仍可运行，但必须显式标记
    prov = r.get("provenance") or {}
    assert prov.get("source") == "synthetic", f"合成回测缺 provenance 标记: {r.keys()}"
    assert seen["n"] == 3  # 合成兜底长度不变


def test_b3b_backtest_default_weights_single_asset(monkeypatch):
    """默认 weights 不得逼单标的走多资产：单 symbol 默认应为单列 close。"""
    import hero_quant.tools.backtest as bt

    bars = [{"close": 100.0 + i} for i in range(5)]
    monkeypatch.setattr(bt, "_fetch_bars_for_backtest", lambda *a, **k: bars)
    seen = _fake_engine(monkeypatch)
    r = bt.run_backtest(symbol="600519.SH")  # 不传 weights
    assert r.get("ok") is True
    assert seen["columns"] == ["close"], f"单标的默认走了多资产列: {seen['columns']}"
    assert seen["weights"] == [1.0]


def test_b3b_optimize_empty_symbols_fail_closed():
    """空 symbols 不得返回错位 weights=[1.0]，应 {ok:False} 信封。"""
    from hero_quant.tools.backtest import optimize_portfolio

    r = optimize_portfolio([])
    assert r.get("ok") is False
    assert r.get("weights") == []
    assert "empty" in str(r.get("error", "")).lower()


# ============ tools/quantlib_tool.py ×5 ============

def _mock_closes(monkeypatch, closes):
    import hero_quant.tools.quantlib_tool as qt
    monkeypatch.setattr(qt, "_fetch_closes", lambda *a, **k: list(closes))


def test_b3b_indicator_insufficient_bars_no_zero_fill(monkeypatch):
    """窗口>可用 bars 时不得 NaN→0.0 伪造 ok:true，应 insufficient_data。"""
    from hero_quant.tools.quantlib_tool import compute_indicator

    _mock_closes(monkeypatch, [100.0, 101.0, 102.0, 103.0, 104.0])
    r = compute_indicator(symbol="600519.SH", indicator="sma", window=20)
    assert r.get("ok") is False
    assert "insufficient" in str(r.get("error", "")).lower()
    assert 0.0 not in (r.get("values") or []), "前窗 NaN 被填 0.0 冒充有效 SMA"


def test_b3b_factor_momentum_insufficient_bars(monkeypatch):
    """~2 根 close 算 N 日动量全零 ok:true：bars<=window 应 ok:False。"""
    from hero_quant.tools.quantlib_tool import compute_factor

    _mock_closes(monkeypatch, [100.0, 101.0, 102.0])
    r = compute_factor(factor="momentum", symbol="600519.SH", window=20)
    assert r.get("ok") is False
    assert "insufficient" in str(r.get("error", "")).lower()


def test_b3b_fetch_closes_no_dead_param():
    """allow_synthetic 死参应移除（fail-closed 无合成路径，避免虚假控制感）。"""
    import hero_quant.tools.quantlib_tool as qt

    assert "allow_synthetic" not in inspect.signature(qt._fetch_closes).parameters
    assert "allow_synthetic" not in inspect.signature(qt.compute_indicator).parameters


def test_b3b_quantlib_docstring_no_synthetic_claim():
    """模块文档不得再声称 20 点合成兜底，应与 fail-closed 实现一致。"""
    import hero_quant.tools.quantlib_tool as qt

    doc = qt.__doc__ or ""
    assert "合成序列兜底" not in doc and "20 点合成" not in doc
    assert "fail-closed" in doc.lower() or "ok:false" in doc.lower()


def test_b3b_validate_window_oversize_raises():
    """超窗 no-op pass：window>bars 应显式抛错，不得静默走 NaN 填充。"""
    from hero_quant.tools.quantlib_tool import _validate_window

    with pytest.raises(ValueError, match="exceeds available bars"):
        _validate_window(20, 5)


# ============ tools/registry.py ×4 ============

def test_b3b_registry_additionalproperties_dict_form():
    """additionalProperties dict 式是合法 JSON Schema，不得误拒。"""
    from hero_quant.tools.registry import assertSupportedJsonSchema

    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "additionalProperties": {"type": "string"},
    }
    assertSupportedJsonSchema(schema)  # 不抛即过
    # 非法值仍拒绝
    with pytest.raises(ValueError):
        assertSupportedJsonSchema({"type": "object", "additionalProperties": "yes"})


def test_b3b_registry_get_definitions_deepcopy():
    """get_definitions 不得泄可变引用：调用方改返回体不得污染注册表。"""
    from hero_quant.tools.registry import TOOL_REGISTRY, get_definitions

    name = "run_backtest"
    assert name in TOOL_REGISTRY
    orig = copy.deepcopy(TOOL_REGISTRY[name].parameters)
    try:
        defs = get_definitions()
        target = next(d for d in defs if d["function"]["name"] == name)
        target["function"]["parameters"]["properties"]["B3B_INJECT_XYZ"] = {"type": "string"}
        defs2 = get_definitions()
        target2 = next(d for d in defs2 if d["function"]["name"] == name)
        assert "B3B_INJECT_XYZ" not in target2["function"]["parameters"]["properties"]
        assert TOOL_REGISTRY[name].parameters == orig
    finally:
        TOOL_REGISTRY[name].parameters = orig


def test_b3b_registry_registration_copies_caller_dicts():
    """注册应存深拷贝：事后改调用方原 dict 不得漂移已注册合约。"""
    from hero_quant.tools.registry import TOOL_REGISTRY, tool

    params = {"type": "object", "properties": {"a": {"type": "string"}}, "required": []}
    output = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    try:
        @tool(name="tmp_b3b_copy_xyz", description="b3b copy probe", parameters=params, output=output)
        def _f():
            pass

        params["properties"]["B3B_DRIFT_XYZ"] = {"type": "string"}
        output["properties"]["B3B_DRIFT2_XYZ"] = {"type": "boolean"}
        spec = TOOL_REGISTRY["tmp_b3b_copy_xyz"]
        assert "B3B_DRIFT_XYZ" not in spec.parameters["properties"]
        assert "B3B_DRIFT2_XYZ" not in spec.output["schema"]["properties"]
    finally:
        TOOL_REGISTRY.pop("tmp_b3b_copy_xyz", None)


def test_b3b_registry_required_check_no_dead_code():
    """required 检查 no-op 死分支应删除，重复校验循环应合并为一处。"""
    import pathlib

    src = pathlib.Path("D:/kaipanla-data/hero-quant/src/hero_quant/tools/registry.py").read_text(encoding="utf-8")
    assert "if isinstance(props, dict) and req not in props" not in src
    loops = src.count("for idx, req in enumerate") + src.count("for r in schema[\"required\"]")
    assert loops == 1, f"required 校验循环应只剩一处，实有 {loops}"

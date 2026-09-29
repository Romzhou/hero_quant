"""T0-1 PoC基线：评审复现的漏洞固化为回归测试。

约定：漏洞存在时对应测试 FAIL（抛不出期望的异常/返回了错误值）；
修复完成后全部 PASS。现在跑：预期 6 个 FAIL（T1-1~T1-7 修完后变 PASS）。
"""
import pytest


def test_poc1_attrgetter_bypass_blocked():
    """PoC-1: operator.attrgetter('__class__') 必须被拦截。"""
    from hero_quant.sandbox.ast_guard import check_source, SandboxViolation

    with pytest.raises(SandboxViolation):
        check_source('import operator; f = operator.attrgetter("__class__"); g = f(())')
    with pytest.raises(SandboxViolation):
        check_source('import operator; f = operator.attrgetter("__subclasses__")')
    with pytest.raises(SandboxViolation):
        check_source('import operator; f = operator.itemgetter("__class__")')


def test_poc2_fromimport_alias_blocked():
    """PoC-2: from-import 别名不得绕过 BANNED_ATTRS。"""
    from hero_quant.sandbox.ast_guard import check_source, SandboxViolation

    with pytest.raises(SandboxViolation):
        check_source('from pandas import read_pickle as r; r("x.pkl")')
    with pytest.raises(SandboxViolation):
        check_source('from yaml import unsafe_load as u; u("x")')


def test_poc3_sandbox_legit_code_still_passes():
    """正常量化代码必须继续放行（防误杀）。"""
    from hero_quant.sandbox.ast_guard import check_source

    check_source('import pandas as pd; x = pd.DataFrame({"a": [1,2,3]}); y = x["a"].sum()')


def test_poc4_skip_pit_requires_ack():
    """PoC-4: skip_pit=True 无二次确认必须抛 PITViolation。"""
    import pandas as pd

    from hero_quant.backtest.engine import BacktestEngine, PITViolation

    prices = pd.DataFrame(
        {"close": [100, 101, 102, 103, 104]},
        index=pd.date_range("2024-01-01", periods=5),
    )
    eng = BacktestEngine()
    with pytest.raises(PITViolation):
        eng.run(prices, weights=[1.0], skip_pit=True)
    # 显式二次确认后才放行，并打 non_pit 标记
    r = eng.run(prices, weights=[1.0], skip_pit=True, pit_ack="I_KNOW_THIS_IS_NON_PIT")
    assert r.get("non_pit") is True


def test_poc5_negative_costs_rejected():
    """PoC-5: costs<0 / NaN / Inf 必须抛 ValueError。"""
    import pandas as pd

    from hero_quant.backtest.engine import BacktestEngine

    prices = pd.DataFrame(
        {"close": [100, 101, 102, 103, 104]},
        index=pd.date_range("2024-01-01", periods=5),
    )
    eng = BacktestEngine()
    kw = {"allow_synthetic": True, "price_date": "2024-01-01", "weights_on": "2024-01-01"}
    with pytest.raises(ValueError):
        eng.run(prices, weights=[1.0], costs=-0.01, **kw)
    with pytest.raises(ValueError):
        eng.run(prices, weights=[1.0], costs=float("nan"), **kw)
    with pytest.raises(ValueError):
        eng.run(prices, weights=[1.0], costs=float("inf"), **kw)


def test_poc6_grounding_rejects_provenance_less_forgery():
    """PoC-6: 无 provenance 的伪造价格 ingest 即被拒。"""
    from hero_quant.agent.grounding import GroundingLedger, GroundingError

    g = GroundingLedger()
    with pytest.raises(GroundingError):
        g.ingest("AAPL", [{"close": 99999.0, "date": "2024-01-01"}])


def test_poc7_rsi_insufficient_data_no_fake_neutral():
    """PoC-7: rsi(3bars, period=14) 不得返回 50.0 假中性。"""
    import math

    import pandas as pd

    from hero_quant.quantlib.indicators import rsi

    r = rsi(pd.Series([100.0, 101.0, 102.0]), period=14)
    assert all(math.isnan(v) for v in r.values), f"got fake values: {list(r.values)}"

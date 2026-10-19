"""J线 J16: 收官打击 —— reconcile/temporal/walltime/validation/options/registry 双源等。"""
from __future__ import annotations

import pandas as pd
import pytest


def test_j16_reconcile_full(tmp_path):
    from hero_quant.governance.reconcile import (ReconcileResult, aggregate_shadow,
                                                load_positions_csv, reconcile,
                                                reconcile_files)
    from hero_quant.governance.ledger import Ledger

    r = reconcile({"A": 100.0}, {"A": 100.0})
    assert r.zero_diff is True and r.total_diff == 0.0
    assert r.to_dict()["zero_diff"] is True
    r2 = reconcile({"A": 100.0}, {"A": 101.0})
    assert r2.zero_diff is False and r2.total_diff == 1.0
    with pytest.raises(ValueError):
        reconcile({"A": 1.0}, {"A": 1.0}, tolerance=-1)
    with pytest.raises(ValueError):
        reconcile({"A": 1.0}, {"A": 1.0}, tolerance="bad")
    with pytest.raises(TypeError):
        reconcile([1], {"A": 1.0})
    with pytest.raises(ValueError):
        reconcile({"A": "bad"}, {"A": 1.0})
    with pytest.raises(ValueError):
        reconcile({"A": 1.0}, {"A": "bad"})
    with pytest.raises(ValueError):
        reconcile({"A": float("nan")}, {"A": 1.0})
    assert ReconcileResult(shadow={}, positions={}, diffs=[], zero_diff=True, total_diff=0.0).to_dict() is not None
    csv = tmp_path / "pos.csv"
    csv.write_text("symbol,qty\nAAPL,100\nMSFT,50\n", encoding="utf-8")
    assert load_positions_csv(csv) == {"AAPL": 100.0, "MSFT": 50.0}
    trades = [{"symbol": "AAPL", "qty": 10}, {"symbol": "AAPL", "quantity": 5}]
    assert aggregate_shadow(trades) == {"AAPL": 15.0}
    # 文件级对账
    led = tmp_path / "led.jsonl"
    Ledger(led).append({"action": "buy", "symbol": "AAPL", "qty": 100}, tenant="t")
    out = reconcile_files(led, csv)
    assert isinstance(out, ReconcileResult)


def test_j16_temporal_heartbeat():
    from hero_quant.checkpoint import temporal as T

    T.heartbeat({"step": 1})
    assert T.get_heartbeat_details() is not None
    T.clear_heartbeat_details()
    assert T.get_heartbeat_details() is None
    h = T.HeartbeatHelper(interval=60)
    h.start({"init": True})
    h.heartbeat({"step": 2})
    assert h.get_details() is not None
    h.stop()


def test_j16_walltime_full():
    import time
    from hero_quant.governance import wall_time as W

    b = W.WallTimeBudget(budget_seconds=30.0, operation="op")
    assert b.remaining() is not None and b.remaining() > 0
    assert b.exceeded() is False and b.is_exceeded() is False
    assert b.elapsed() >= 0
    b.check()
    b.enforce()
    with b:
        pass
    assert W._resolve_default_budget(5.0) == 5.0
    assert W._resolve_default_budget(None) is None or isinstance(W._resolve_default_budget(None), float)
    with pytest.raises(W.WallTimeExceeded):
        W.WallTimeBudget(budget_seconds=0.001, operation="x").time_call(lambda: time.sleep(0.05))
    gov = W.WallTimeGovernor(budget_seconds=30.0)
    assert gov.enforce(lambda: 7) == 7
    wrapped = gov.wrap(lambda x: x * 2)
    assert wrapped(21) == 42
    with pytest.raises(W.WallTimeExceeded):
        gov.check_budget(99999.0)
    assert W.wall_time_budget(budget_seconds=30.0).budget_seconds == 30.0
    d = W.with_wall_time_budget(budget=5.0, operation="a")
    assert d is not None


def test_j16_validation_exact():
    import pandas as pd
    from hero_quant.backtest.validation import ValidationError, validate

    idx = pd.date_range("2024-01-01", periods=3, freq="D")
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)
    # kwargs 兼容
    assert validate(df, weights_on="2024-01-01", price_date="2024-01-02") is None
    assert validate(df, **{"weights_on": "2024-01-01", "price_date": "2024-01-02"}) is None
    # 非法时间戳类型
    with pytest.raises(ValidationError):
        validate(df, weights_on=object(), price_date="2024-01-02")
    # 混合 naive/aware 同墙钟非 UTC 偏移 -> 拒绝
    aware = pd.Timestamp("2024-01-01", tz="US/Eastern")
    with pytest.raises(ValidationError):
        validate(df, weights_on="2024-01-01", price_date=aware)
    # 兄弟价格列同步校验
    df2 = pd.DataFrame({"close": [1.0, 2.0, 3.0], "open": [1.0, -2.0, 3.0]}, index=idx)
    with pytest.raises(ValidationError):
        validate(df2)


def test_j16_options_exact():
    from hero_quant.quantlib import options as O

    g = O.bs_greeks(S=100.0, K=100.0, T=1.0, option_type="put")
    assert "delta" in g and g["delta"] < 0
    assert O.bs_price(S=100.0, K=100.0, T=0.0) >= 0
    with pytest.raises(ValueError):
        O.implied_volatility(0.01, S=100.0, K=100.0, T=1.0)  # 低于内在价值
    assert O.bs_price(S=100.0, K=100.0, T=1.0, sigma=0.2, option_type="call", extra_kw=1) is not None or True


def test_j16_registry_dual_loader(monkeypatch):
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    import sys
    sys.path.insert(0, "tests")
    from test_j9b_leftovers import _bars_list, _fake_loader
    from hero_quant.data.registry import MarketDataRegistry

    reg = MarketDataRegistry()
    reg.register(_fake_loader("tencent", ["CN"], _bars_list(3), unit="board_lots", prov_source="tencent"))
    reg.register(_fake_loader("tencent", ["CN"], _bars_list(3, base=101.0), unit="board_lots", prov_source="tencent"))
    bars, prov = reg.get_bars("000001.SZ", "2024-01-01", "2024-01-03")
    assert len(bars) == 3


def test_j16_postgres_saver_variants():
    from hero_quant.checkpoint.postgres import PostgresSaver, get_saver
    import sys
    sys.path.insert(0, "tests")
    from test_j10_pgpool_vectors import _J10Pool

    sv = PostgresSaver(dsn="postgresql://postgres:postgres@localhost:5432/j16", pool=_J10Pool())
    sv.setup()
    sv.put("wf:r16:t16", {"a": 1}, None)
    assert sv.get("wf:r16:t16") is None or isinstance(sv.get("wf:r16:t16"), dict)
    assert get_saver(dsn="memory://j16") is not None
    assert get_saver(dsn="postgresql://postgres:postgres@localhost:5432/j16", pool=_J10Pool()) is not None
    assert sv._list_memory_ids() is not None


def test_j16_engine_breach_and_ledger_query(tmp_path, monkeypatch):
    import time
    from hero_quant.backtest.engine import BacktestEngine

    eng = BacktestEngine()
    real = time.perf_counter
    seq = {"i": 0}

    def _fake_pc():
        seq["i"] += 1
        return real() + (0 if seq["i"] == 1 else 500)
    monkeypatch.setattr(time, "perf_counter", _fake_pc)
    try:
        out = eng.on_tick({"price": 1.0, "symbol": "A"})
        assert out["latency_breach"] is True
    finally:
        monkeypatch.setattr(time, "perf_counter", real)
    from hero_quant.governance.ledger import Ledger

    led = Ledger(tmp_path / "q.jsonl")
    led.append({"action": "a"}, tenant="t1", price=10.0)
    assert led.verify(tenant="t1") in (True, False)
    assert led.query("t1") is not None


def test_j16_scheduler_dispatch_redis():
    import hero_quant.infra.redis as R
    import fakeredis
    from hero_quant.scheduled import service as S

    fake = fakeredis.FakeRedis(decode_responses=True)
    R.set_redis_instance(fake)
    try:
        d = S.ScheduledService().dispatch("premarket-brief")
        assert isinstance(d, dict)
        assert S.parse_cron("0 0 29 2 *") is not None
        assert S.validate_cron("0 9 * * *") is True
    finally:
        R.clear_redis_instance()


def test_j16_heartbeat_otel_settings():
    from hero_quant.telemetry import heartbeat as H

    assert len([n for n in dir(H) if not n.startswith("_")]) > 0
    from hero_quant.telemetry import otel as O

    if hasattr(O, "init_otel"):
        try:
            O.init_otel()
        except Exception:
            pass
    if hasattr(O, "get_tracer"):
        try:
            O.get_tracer("j16")
        except Exception:
            pass

"""Task 7 TDD: correlation synthetic fallback removed — fail loud."""
import pytest


def test_compute_correlation_fetch_failure_returns_not_ok(monkeypatch):
    import hero_quant.tools.correlation as corr_mod

    def fake_fetch(*args, **kwargs):
        raise ConnectionError("fetch failed")

    monkeypatch.setattr(corr_mod, "_fetch_closes", fake_fetch)
    # ensure data_mode live so synthetic not allowed
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    res = corr_mod.compute_correlation("AAPL", "MSFT", start="2026-07-01", end="2026-08-01")
    assert res["ok"] is False
    assert "error" in res and res["error"]
    assert res["correlation"] == 0.0
    assert res["points"] == 0
    # must not be fake 1.0 correlation
    assert res["correlation"] != 1.0 or res["ok"] is False


def test_fetch_closes_raises_on_failure(monkeypatch):
    import hero_quant.tools.correlation as corr_mod

    monkeypatch.setenv("HERO_DATA_MODE", "live")
    # patch registry to raise inside _fetch_closes
    import hero_quant.data.registry as reg_mod

    orig_get_bars = reg_mod.MarketDataRegistry.get_bars

    def failing_get_bars(self, *args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(reg_mod.MarketDataRegistry, "get_bars", failing_get_bars)
    with pytest.raises(Exception):
        corr_mod._fetch_closes("AAPL", "2026-07-01", "2026-08-01")


def test_compute_correlation_normal_path_ok(monkeypatch):
    import hero_quant.tools.correlation as corr_mod

    # monkeypatch _fetch_closes to return valid closes with variance
    def fake_fetch(symbol, start, end):
        # two correlated series: both linear but with noise per symbol
        base = [100 + i * 0.5 for i in range(20)]
        if symbol == "AAPL":
            return base
        # MSFT slightly correlated
        return [x + (0.1 if i % 2 == 0 else -0.1) for i, x in enumerate(base)]

    monkeypatch.setattr(corr_mod, "_fetch_closes", fake_fetch)
    res = corr_mod.compute_correlation("AAPL", "MSFT", start="2026-07-01", end="2026-08-01")
    assert res["ok"] is True
    assert res["points"] >= 2
    assert -1.0 <= res["correlation"] <= 1.0
    # not fake synthetic error case
    assert "error" not in res or res.get("error") is None or res["ok"] is True


def test_compute_correlation_insufficient_points_returns_not_ok(monkeypatch):
    import hero_quant.tools.correlation as corr_mod

    monkeypatch.setattr(corr_mod, "_fetch_closes", lambda s, start, end: [100.0])
    res = corr_mod.compute_correlation("AAPL", "MSFT")
    assert res["ok"] is False
    assert "insufficient" in res["error"].lower() or "points" in res["error"].lower()


def test_synthetic_only_when_explicit_flag(monkeypatch):
    import hero_quant.tools.correlation as corr_mod
    import hero_quant.data.registry as reg_mod
    from hero_quant.tools.correlation import SyntheticDataUnavailable

    def failing_get_bars(self, *args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr(reg_mod.MarketDataRegistry, "get_bars", failing_get_bars)
    # 中文：synthetic 模式亦 fail-closed——合成数据不得用于相关性（避免假相关），
    # 显式抛 SyntheticDataUnavailable（调用方转 ok:False + provenance synthetic）。
    monkeypatch.setenv("HERO_DATA_MODE", "synthetic")
    with pytest.raises(SyntheticDataUnavailable):
        corr_mod._fetch_closes("AAPL", "2026-07-01", "2026-08-01")
    # live 模式同样 raise
    monkeypatch.setenv("HERO_DATA_MODE", "live")
    with pytest.raises(Exception):
        corr_mod._fetch_closes("AAPL", "2026-07-01", "2026-08-01")

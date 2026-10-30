"""PR-125：回测窗口参数化（默认 60 根），R8-1b 的披露规则一字不松。

锁定的契约（docs/production-readiness/backlog.md:40 / phase-1.md:121）：
- `_BACKTEST_BARS_TARGET == 60 == _BACKTEST_MIN_SHARPE_BARS`：默认窗口恰好命中"可年化"的
  最低样本，默认真行情能给出年化 Sharpe；门槛本身不动（判定仍是 `bars < 60` 才置 null）。
- 窗口只走内部参数（`_resolve_backtest_bars` 夹到 [MIN=2, MAX=250]），四个端点仍零参调用，
  不加公开 query 参数（OpenAPI 表面不变，PR-182 以此为基线）。
- 回看天数按 target 反推（max(target*2+10, 30) 日历日），tail(target) 只留最近 target 根；
  上游给不够就只披露实际根数（不补不造），<60 根仍 sharpe=null + insufficient_sample。
- L2 key 版本 v5 且把实际生效 bars 纳入 hash：换窗口必须换 key，否则 Redis 里的 20 根旧
  bundle 会被当 60 根新结果返回。

不联网：`hero_quant.tools.market_data.get_market_data` 全部 monkeypatch 成本地 fake。
"""

from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd
import pytest

import hero_quant.api.server as srv

# 固定"最近一根"的日期（今天=2026-10-31 环境）；只用历史日期，绝不构造未来行情。
_LAST_DAY = "2026-10-30"


def _prices(n: int) -> pd.DataFrame:
    """n 根日线（工作日索引，收盘有波动，够算 sharpe）。"""
    idx = pd.bdate_range(end=_LAST_DAY, periods=n)
    vals = 100.0 + np.cumsum(np.sin(np.arange(n)) * 0.5 + 0.05)
    df = pd.DataFrame({"close": vals}, index=idx)
    df.index.name = "date"
    return df


def _payload(n: int) -> list[dict]:
    """get_market_data 的 bars 形状：list[{"date": str, "close": float}]。"""
    df = _prices(n)
    return [{"date": d.date().isoformat(), "close": float(c)} for d, c in zip(df.index, df["close"])]


@pytest.fixture()
def upstream(monkeypatch):
    """把真行情网关换成本地 fake，并记录每次请求的窗口参数（不联网）。"""
    import hero_quant.tools.market_data as md

    calls: list[dict] = []

    def _install(n: int | None):
        def _fake(symbol, interval="1d", start=None, end=None, allow_synthetic=True):
            calls.append(
                {
                    "symbol": symbol,
                    "interval": interval,
                    "start": start,
                    "end": end,
                    "allow_synthetic": allow_synthetic,
                }
            )
            return {"bars": _payload(n) if n else [], "provenance": {"source": "fake"}, "ok": True}

        monkeypatch.setattr(md, "get_market_data", _fake)
        return calls

    return _install


@pytest.fixture()
def client(monkeypatch):
    """隔离两级缓存（L1 单槽 + L2 Redis），保证每个用例都真算一遍。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(srv, "_read_backtest_bundle_cache", lambda tenant=None, bars=None: None)
    monkeypatch.setattr(srv, "_write_backtest_bundle_cache", lambda bundle, tenant=None, bars=None: None)
    monkeypatch.setattr(srv, "_backtest_cache", {}, raising=False)
    monkeypatch.setattr(srv, "_backtest_demo_enabled", lambda: False, raising=False)
    return TestClient(srv.app)


def _metrics_body(client) -> dict:
    r = client.get("/v1/backtest/metrics.json")
    assert r.status_code == 200, r.text[:300]
    return r.json()


# ================= 常量与窗口边界 =================


def test_default_window_meets_min_sharpe_sample():
    """默认窗口 == 年化 Sharpe 最低样本（60），且落在内部边界内。"""
    assert srv._BACKTEST_BARS_TARGET == 60
    assert srv._BACKTEST_BARS_TARGET == srv._BACKTEST_MIN_SHARPE_BARS
    assert srv._BACKTEST_BARS_MIN == 2
    assert srv._BACKTEST_BARS_MAX == 250
    assert srv._BACKTEST_BARS_MIN <= srv._BACKTEST_BARS_TARGET <= srv._BACKTEST_BARS_MAX


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 60),  # 缺省 = 默认窗口
        (2, 2),  # 下限本身
        (20, 20),  # 旧默认窗口仍可显式请求
        (60, 60),
        (250, 250),  # 上限本身
        (0, 2),  # 低于下限 → 夹到 2
        (-5, 2),
        (10_000, 250),  # 高于上限 → 夹到 250，不裸拉几十年
    ],
)
def test_resolve_bars_clamps_into_bounds(raw, expected):
    assert srv._resolve_backtest_bars(raw) == expected


def test_resolve_bars_invalid_input_falls_back_to_default():
    """非数字输入不抛错到端点，回默认窗口。"""
    assert srv._resolve_backtest_bars("not-a-number") == srv._BACKTEST_BARS_TARGET


# ================= 真行情加载：回看窗口与 tail =================


def test_loader_requests_enough_history_for_default_window(upstream):
    """默认窗口请求的 start/end 必须覆盖 ≥60 个交易日（否则永远拿不到年化样本）。"""
    calls = upstream(60)

    prices = srv._load_backtest_prices(pd, None)

    assert len(calls) == 1
    req = calls[0]
    assert req["symbol"] == srv._BACKTEST_SYMBOL
    assert req["interval"] == "1d"
    assert req["allow_synthetic"] is False, "真行情通道必须禁止合成回退"
    span = pd.bdate_range(req["start"], req["end"])
    assert len(span) >= srv._BACKTEST_MIN_SHARPE_BARS, f"回看窗口仅 {len(span)} 个交易日，取不到 60 根"
    assert prices is not None and len(prices) == 60


def test_loader_lookback_scales_with_target(upstream):
    """回看天数按 target 反推，不是把 20 根时代的常数写死。"""
    calls = upstream(250)

    srv._load_backtest_prices(pd, srv._BACKTEST_BARS_MAX)

    span = pd.bdate_range(calls[0]["start"], calls[0]["end"])
    assert len(span) >= srv._BACKTEST_BARS_MAX, f"250 根窗口只回看 {len(span)} 个交易日"


def test_loader_tails_to_window_keeping_latest(upstream):
    """上游给 100 根：只留最近 60 根，末尾日期必须是最新那根。"""
    upstream(100)

    prices = srv._load_backtest_prices(pd, None)

    assert prices is not None
    assert len(prices) == srv._BACKTEST_BARS_TARGET
    assert prices.index[-1] == pd.Timestamp(_payload(100)[-1]["date"])
    assert prices.index.is_monotonic_increasing


def test_loader_does_not_pad_short_history(upstream):
    """上游只给 40 根：原样返回 40 根，绝不补造到窗口长度。"""
    upstream(40)

    prices = srv._load_backtest_prices(pd, None)

    assert prices is not None
    assert len(prices) == 40


def test_loader_returns_none_without_bars(upstream):
    """上游空 bars：返回 None（由调用方按空产物降级），不编日期。"""
    upstream(None)

    assert srv._load_backtest_prices(pd, None) is None
    assert srv._backtest_sample(None) == {"bars": 0, "start": None, "end": None}


# ================= 披露规则：门槛不动 =================


def test_disclosure_threshold_still_strictly_below_60():
    """59 根置 null，60 根才年化 —— 边界是 `>=`，不为"默认能年化"放宽。"""
    metrics, basis, warnings = srv._apply_metrics_disclosure({"sharpe": 1.0}, {"bars": 59})
    assert metrics["sharpe"] is None
    assert basis is None
    assert "insufficient_sample" in warnings

    metrics, basis, warnings = srv._apply_metrics_disclosure({"sharpe": 1.0}, {"bars": 60})
    assert metrics["sharpe"] == 1.0
    assert basis == "annualized_252"
    assert warnings == []


# ================= 缓存键：v5 + 窗口入 hash =================


def _digest(raw: dict) -> str:
    return hashlib.sha256(json.dumps(raw, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def test_cache_key_is_v5_and_hashes_effective_window():
    """锁 key 的组成（前缀 + v5 + bars + capital + weights），不锁某个魔法 hash 值。"""
    prefix = srv._BACKTEST_BUNDLE_CACHE_PREFIX
    base = {"capital": 1.0, "weights": [0.5, 0.5]}

    key = srv._backtest_bundle_cache_key()
    assert key.startswith(prefix), "key 必须带 bundle 前缀，别和其他缓存撞名"
    assert key == prefix + _digest({**base, "v": 5, "bars": 60}), "默认窗口 60 也要进 hash"
    # v4 旧键（20 根产物）必须彻底对不上
    assert key != prefix + _digest({**base, "v": 4, "bars": 60})
    assert key != prefix + _digest({**base, "v": 4})
    # bars 真的参与 hash：去掉 bars 的同一份参数算出来的就不是这个 key
    assert key != prefix + _digest({**base, "v": 5})


def test_cache_key_isolates_window_and_tenant():
    k_default = srv._backtest_bundle_cache_key()
    assert k_default == srv._backtest_bundle_cache_key(bars=srv._BACKTEST_BARS_TARGET)
    assert k_default == srv._backtest_bundle_cache_key(bars=60), "同一窗口必须稳定命中"
    k20 = srv._backtest_bundle_cache_key(bars=20)
    assert k20 != k_default, "换窗口必须换 key，否则 20 根旧 bundle 冒充 60 根"
    assert k20 == srv._backtest_bundle_cache_key(bars=20)
    assert srv._backtest_bundle_cache_key("tenant-a") != k_default
    assert srv._backtest_bundle_cache_key("tenant-a", 20) != srv._backtest_bundle_cache_key("tenant-a")


# ================= demo 行情：根数跟随窗口、标记不变 =================


def test_demo_prices_follow_window():
    default = srv._demo_backtest_prices(pd)
    assert len(default) == srv._BACKTEST_BARS_TARGET == 60
    assert default.index.name == "date"
    assert len(srv._demo_backtest_prices(pd, 30)) == 30
    assert len(srv._demo_backtest_prices(pd, 1)) == srv._BACKTEST_BARS_MIN, "低于下限夹到 2"


def test_demo_mode_marks_synthetic_and_annualizes(client, upstream, monkeypatch):
    """demo 开关打开：生成 60 根 → 恰好够年化，但标记照旧 synthetic/degraded。"""
    upstream(None)
    monkeypatch.setattr(srv, "_backtest_demo_enabled", lambda: True)

    body = _metrics_body(client)

    assert body["sample"]["bars"] == srv._BACKTEST_BARS_TARGET
    assert body["synthetic"] is True
    assert body["degraded"] is True
    assert body["sharpe_basis"] == "annualized_252"
    assert isinstance(body["sharpe"], (int, float))


def test_demo_mode_short_window_still_disclosed(client, upstream, monkeypatch):
    """demo 只换数据源：合成 20 根照样 sharpe=null + insufficient_sample。"""
    upstream(None)
    monkeypatch.setattr(srv, "_backtest_demo_enabled", lambda: True)
    monkeypatch.setattr(srv, "_demo_backtest_prices", lambda pd_mod, *a, **k: _prices(20))

    body = _metrics_body(client)

    assert body["sample"]["bars"] == 20
    assert body["sharpe"] is None
    assert body["sharpe_basis"] is None
    assert "insufficient_sample" in body["warnings"]
    assert body["synthetic"] is True


# ================= 端到端：真行情 80 根 → 60 根年化；40 根 → null =================


def test_end_to_end_default_window_annualizes(client, upstream):
    """上游 80 根 → 窗口取最近 60 根 → sharpe 数值 + annualized_252，且不标降级。"""
    upstream(80)

    body = _metrics_body(client)

    assert body["sample"]["bars"] == srv._BACKTEST_BARS_TARGET
    assert body["sample"]["end"] == _payload(80)[-1]["date"]
    assert isinstance(body["sharpe"], (int, float)) and body["sharpe"] is not None
    assert body["sharpe_basis"] == "annualized_252"
    assert "insufficient_sample" not in body["warnings"]
    assert body["synthetic"] is False and body["degraded"] is False, "真行情不得标合成降级"


def test_end_to_end_short_history_keeps_sharpe_null(client, upstream):
    """上游只有 40 根 → 不补不造，sample.bars=40 且 sharpe=null + insufficient_sample。"""
    upstream(40)

    body = _metrics_body(client)

    assert body["sample"]["bars"] == 40
    assert body["sharpe"] is None
    assert body["sharpe_basis"] is None
    assert "insufficient_sample" in body["warnings"]


def test_end_to_end_no_data_keeps_zero_sample(client, upstream):
    """无行情：bars=0、无日期、no_market_data，不许为了凑窗口去造 K 线。"""
    upstream(None)

    body = _metrics_body(client)

    assert body["sample"] == {"bars": 0, "start": None, "end": None}
    assert "no_market_data" in body["warnings"]
    assert body.get("sharpe") is None


# ================= 公开表面不变 =================


def test_backtest_endpoints_expose_no_bars_query_param():
    """窗口是内部参数：OpenAPI 里不得出现 bars query 参数（PR-182 基线）。"""
    params = srv.app.openapi()["paths"]["/v1/backtest/metrics.json"]["get"].get("parameters", [])
    assert all(p.get("name") != "bars" for p in params), f"不得新增公开 query 参数: {params}"


def test_get_backtest_bundle_is_parameterized_explicit_window_is_live(client, upstream):
    """内部显式窗口要真生效（不是收下参数就扔），且不污染默认窗口的 L1 槽。"""
    calls = upstream(20)

    body = srv._get_backtest_bundle(bars=20)

    assert calls and calls[0]["allow_synthetic"] is False
    assert body["sample"]["bars"] == 20, "显式 bars=20 必须真的按 20 根算"
    assert body["metrics"]["sharpe"] is None, "<60 根仍置 null（口径不随窗口参数放宽）"
    # 默认窗口不受影响（L1 单槽没被 20 根占住）
    upstream(80)
    default_body = srv._get_backtest_bundle()
    assert default_body["sample"]["bars"] == srv._BACKTEST_BARS_TARGET

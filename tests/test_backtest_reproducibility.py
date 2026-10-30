"""PR-124：回测可复现 —— config_hash / data_fingerprint / result_hash（+ 输入指纹）。

锁定的契约：
- `canonical_json` 浮点 12 位归一：1.0 与 1.0+1e-13 同 hash；差异 >1e-12 必然不同 hash；
- 同「数据 + 代码 + 配置」重跑两次 `_compute_backtest_bundle` → `result_hash` 完全一致，
  metrics 数值按 1e-9 容差相等（本轮不比对跨平台比特级一致，见模块 docstring 的验收口径）；
- 改 weights / bars → `config_hash` 变；改一根 close（非末位抖动量级）→ `data_fingerprint`
  与 `result_hash` 都变，而 `config_hash` 不变（配置没动）；
- `code_commit` 在无 git 环境返回 None：响应 `code_commit: null` + warnings 含
  `code_commit_unknown`（只加一次），**绝不编假 sha**；
- 无行情/静态兜底：`reproducible=false`、`result_hash=None`、带 reason，不出现 64 位假 sha；
- L2 缓存往返 `reproducibility` 不丢；缺 `reproducibility` 的旧 payload 读回即 miss。

D1 决策（本轮边界，别误解证明力）：`data_fingerprint` 绑定的是「本次实际喂给引擎的价格序列」，
**不是 PIT 快照**（PR-122 全量 PIT 仍被依赖审批阻塞）。所以这里只锁「同输入 ⇒ 同指纹」，
不断言「与历史某时点的 PIT 快照等价」—— 那条绑定仍缺。
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd
import pytest

import hero_quant.api.server as srv
from hero_quant.backtest.reproducibility import (
    HASH_VERSION,
    canonical_json,
    code_commit,
    config_hash,
    data_fingerprint,
    reset_code_commit_cache,
    result_hash,
)

_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_REPRO_KEYS = {"config_hash", "result_hash", "data_fingerprint", "code_commit", "hash_version", "reproducible"}


def _prices(n: int = 60, *, tweak_at: int | None = None, tweak: float = 0.0) -> pd.DataFrame:
    """n 根工作日日线；tweak_at 指定改第几根 close（只改一根，且不是末位噪声）。"""
    idx = pd.bdate_range(end="2026-10-30", periods=n)  # 只用历史日期，不构造未来行情
    vals = 100.0 + np.cumsum(np.sin(np.arange(n)) * 0.5 + 0.05)
    if tweak_at is not None:
        vals = vals.copy()
        vals[tweak_at] += tweak
    df = pd.DataFrame({"close": vals}, index=idx)
    df.index.name = "date"
    return df


@pytest.fixture(autouse=True)
def _clean_commit_cache():
    """commit 缓存在模块级：每个用例前后都清，否则「无 git → None」会串到别的用例。"""
    reset_code_commit_cache()
    yield
    reset_code_commit_cache()


@pytest.fixture()
def client(monkeypatch):
    """隔离两级缓存（L1 单槽 + L2 Redis），保证每个用例都真算一遍。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(srv, "_read_backtest_bundle_cache", lambda tenant=None, bars=None: None)
    monkeypatch.setattr(srv, "_write_backtest_bundle_cache", lambda bundle, tenant=None, bars=None: None)
    monkeypatch.setattr(srv, "_backtest_cache", {}, raising=False)
    monkeypatch.setattr(srv, "_backtest_demo_enabled", lambda: False, raising=False)
    return TestClient(srv.app)


@pytest.fixture()
def upstream(monkeypatch):
    """把行情 loader 换成本地 fake（不联网）；返回可切换价格的可调用对象。"""
    state = {"df": _prices()}

    def _set(df: pd.DataFrame | None) -> None:
        state["df"] = df

    # PR-125：loader 新增内部窗口参数（target），用 *a/**k 吃掉，不影响本文件断言。
    monkeypatch.setattr(
        srv, "_load_backtest_prices", lambda pd_mod, *a, **k: (state["df"].copy() if state["df"] is not None else None)
    )
    monkeypatch.setattr(srv, "_backtest_demo_enabled", lambda: False, raising=False)
    return _set


class _FakeRedis:
    """最小 sync Redis 替身：只实现 get/set（bundle 缓存用到的全部）。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):  # noqa: A002 - 对齐 redis 客户端签名
        self.store[key] = value
        return True


@pytest.fixture()
def fake_redis(monkeypatch):
    """把 infra.redis.get_redis_sync 换成本地 fake（不真连 Redis）。"""
    import hero_quant.infra.redis as rmod

    fake = _FakeRedis()
    monkeypatch.setattr(rmod, "get_redis_sync", lambda: fake)
    return fake


# ===================== canonical_json：浮点归一与确定性序列化 =====================


def test_canonical_json_treats_sub_1e12_noise_as_equal():
    """1e-13 级抖动是「同一个数」：归一后字节相同 → 同 hash。"""
    assert canonical_json({"x": 1.0}) == canonical_json({"x": 1.0 + 1e-13})
    assert canonical_json({"x": [0.5, 1.25]}) == canonical_json({"x": [0.5 + 1e-13, 1.25 + 1e-14]})
    # 真实差异（> 1e-12）必须被保留，不能被归一化吃掉
    assert canonical_json({"x": 1.0}) != canonical_json({"x": 1.0 + 1e-11})
    assert canonical_json({"x": 1.0}) != canonical_json({"x": 1.0 + 1e-9})


def test_canonical_json_is_key_order_and_space_insensitive():
    """键序/空格差异不得影响文本（否则 hash 会随 dict 构造顺序漂移）。"""
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1}) == '{"a":2,"b":1}'


def test_canonical_json_maps_nonfinite_to_null_and_rejects_unknown_types():
    """NaN/inf 不是合法 JSON：统一折成 null；不支持的类型硬失败，不做隐式 str()。"""
    assert canonical_json({"x": float("nan")}) == canonical_json({"x": None})
    assert canonical_json({"x": float("inf")}) == canonical_json({"x": None})
    assert canonical_json({"x": -0.0}) == canonical_json({"x": 0.0})
    with pytest.raises(TypeError):
        canonical_json({"x": object()})


# ===================== config_hash：影响结果的配置才入 hash =====================


def test_config_hash_covers_result_affecting_fields():
    """weights / bars / engine_version 任何一个变都必须换 config_hash。"""
    cfg = srv._backtest_config()
    assert set(cfg) >= {"symbol", "interval", "bars", "adjust", "initial_capital", "weights", "engine", "engine_version"}
    base = config_hash(cfg)
    assert base == srv._backtest_config_hash()
    assert len(base) == 32 and _HEX32.match(base), "指纹必须是 32 hex（不是 64 位假 sha）"
    assert config_hash({**cfg, "weights": [0.6, 0.4]}) != base
    assert config_hash({**cfg, "bars": 30}) != base
    assert config_hash({**cfg, "engine_version": "0.0.0-test"}) != base
    assert srv._backtest_config_hash(20) != srv._backtest_config_hash(60), "窗口是配置的一部分"


def test_config_hash_rejects_missing_and_volatile_keys():
    """少传结果相关配置、或塞时间戳/租户 → 直接报错（否则指纹不可比）。"""
    cfg = srv._backtest_config()
    broken = dict(cfg)
    broken.pop("engine_version")
    with pytest.raises(ValueError):
        config_hash(broken)
    with pytest.raises(ValueError):
        config_hash({**cfg, "tenant": "t-1"})
    with pytest.raises(ValueError):
        config_hash({**cfg, "timestamp": "2026-10-30T00:00:00Z"})


# ===================== data_fingerprint：绑定本次输入（非 PIT 快照） =====================


def test_data_fingerprint_none_or_empty_returns_none():
    """空/None 不造假 hash —— 空输入算出来的 sha 会让空壳看起来可复现。"""
    assert data_fingerprint(None) is None
    assert data_fingerprint([]) is None
    assert data_fingerprint(pd.DataFrame({"close": []}, index=pd.DatetimeIndex([]))) is None


def test_data_fingerprint_same_series_same_hash_across_shapes():
    """DataFrame 与 list[dict] 只要序列相同，指纹必须相同（同源同值同指纹）。"""
    df = _prices(20)
    rows = [{"date": d.date().isoformat(), "close": float(c)} for d, c in zip(df.index, df["close"])]
    fp = data_fingerprint(df)
    assert fp == data_fingerprint(rows)
    assert len(fp) == 32 and _HEX32.match(fp)


def test_data_fingerprint_is_order_sensitive_and_price_sensitive():
    """时间序列换序 = 不同输入；改一根 close = 不同输入。"""
    df = _prices(20)
    assert data_fingerprint(df) != data_fingerprint(df.iloc[::-1])
    assert data_fingerprint(df) != data_fingerprint(_prices(20, tweak_at=10, tweak=5.0))


def test_data_fingerprint_returns_none_without_close_column():
    """没有 close 列就无法按「日期 + close」定指纹：返回 None，不拿别的列凑数。"""
    assert data_fingerprint(pd.DataFrame({"open": [1.0, 2.0]}, index=pd.bdate_range(end="2026-10-30", periods=2))) is None


# ===================== result_hash：只吃产物，不吃耗时/缓存标记 =====================


def test_result_hash_ignores_volatile_metrics_and_tracks_tearsheet():
    """耗时/缓存命中不进 hash；tearsheet 变了必须换 hash。"""
    m = {"cumulative_return": 0.12, "volatility": 0.03}
    base = result_hash(metrics=m, positions_csv="date,close\n2026-01-05,1.0\n", tearsheet="<html/>")
    assert base == result_hash(
        metrics={**m, "elapsed_ms": 3, "cache_hit": True}, positions_csv="date,close\n2026-01-05,1.0\n", tearsheet="<html/>"
    )
    assert base != result_hash(metrics=m, positions_csv="date,close\n2026-01-05,1.0\n", tearsheet="<html>2</html>")


# ===================== 重跑一致性：同数据 + 同配置 → 同 result_hash =====================


def _one_compute() -> dict:
    return srv._compute_backtest_bundle()


def test_recompute_yields_identical_result_hash(upstream):
    """同价格 + 同配置跑两次：result_hash / data_fingerprint / config_hash 全等，指标 1e-9 内相等。"""
    b1 = _one_compute()
    b2 = _one_compute()

    r1, r2 = b1["reproducibility"], b2["reproducibility"]
    assert r1["result_hash"] == r2["result_hash"], "同输入重跑必须给出同一个产物指纹"
    assert r1["data_fingerprint"] == r2["data_fingerprint"]
    assert r1["config_hash"] == r2["config_hash"]
    assert r1["hash_version"] == HASH_VERSION == 1
    assert r1["reproducible"] is True
    assert "reason" not in r1, "可复现时不该有降级原因"

    assert b1["csv"] == b2["csv"]
    assert set(b1["metrics"]) == set(b2["metrics"])
    for key, value in b1["metrics"].items():
        if isinstance(value, float):
            assert b2["metrics"][key] == pytest.approx(value, abs=1e-9, rel=1e-9), f"metrics.{key} 超出 1e-9 容差"
        else:
            assert b2["metrics"][key] == value


def test_price_change_moves_input_and_result_hash_not_config_hash(upstream):
    """改一根 close：data_fingerprint / result_hash 变，config_hash 不变。"""
    base = _one_compute()
    upstream(_prices(60, tweak_at=10, tweak=5.0))
    moved = _one_compute()

    assert moved["reproducibility"]["config_hash"] == base["reproducibility"]["config_hash"], "配置没动"
    assert moved["reproducibility"]["data_fingerprint"] != base["reproducibility"]["data_fingerprint"]
    assert moved["reproducibility"]["result_hash"] != base["reproducibility"]["result_hash"]


def test_bars_change_moves_config_hash(upstream):
    """换窗口 = 换配置：config_hash 变（同价格序列下 data_fingerprint 不变）。"""
    base = _one_compute()
    upstream(_prices(40))
    shorter = srv._compute_backtest_bundle(bars=40)

    assert shorter["reproducibility"]["config_hash"] != base["reproducibility"]["config_hash"]
    assert shorter["reproducibility"]["data_fingerprint"] != base["reproducibility"]["data_fingerprint"]


# ===================== code_commit：拿不到就是 null，绝不编假 sha =====================


def test_code_commit_returns_none_without_git(monkeypatch):
    """非 git 环境（subprocess 抛 FileNotFoundError）→ None，不编假 sha。"""

    def _boom(*a, **k):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr("hero_quant.backtest.reproducibility.subprocess.run", _boom)
    reset_code_commit_cache()

    assert code_commit() is None
    assert code_commit() is None, "失败结果必须缓存，别每次请求都起 subprocess"


def test_endpoint_discloses_unknown_code_commit(client, upstream, monkeypatch):
    """无 git 时响应 code_commit=null 且 warnings 含 code_commit_unknown（只加一次）。"""

    def _boom(*a, **k):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr("hero_quant.backtest.reproducibility.subprocess.run", _boom)
    reset_code_commit_cache()

    r = client.get("/v1/backtest/metrics.json")
    assert r.status_code == 200, r.text[:300]
    body = r.json()

    assert body["reproducibility"]["code_commit"] is None
    assert body["warnings"].count("code_commit_unknown") == 1, f"只许加一次: {body['warnings']}"
    assert body["reproducibility"]["reproducible"] is True, "commit 未知不等于结果不可复现"


# ===================== 无行情 / 静态兜底：不算假 hash =====================


def test_no_input_data_is_explicitly_not_reproducible(upstream):
    """无行情：data_fingerprint/result_hash 为 None，reproducible=false + reason=no_input_data。"""
    upstream(None)
    bundle = _one_compute()
    rep = bundle["reproducibility"]

    assert set(rep) >= _REPRO_KEYS
    assert rep["reproducible"] is False
    assert rep["result_hash"] is None
    assert rep["data_fingerprint"] is None
    assert rep["reason"] == "no_input_data"
    assert rep["config_hash"] and len(rep["config_hash"]) == 32, "配置指纹与是否有行情无关，仍给真值"
    assert not [v for v in rep.values() if isinstance(v, str) and _HEX64.match(v)], "不许出现 64 位假 sha"
    assert "no_market_data" in bundle["warnings"]


def test_static_fallback_is_not_reproducible():
    """静态兜底同样显式不可复现（reason=compute_failed），不伪造产物指纹。"""
    bundle = srv._static_backtest_bundle()
    rep = bundle["reproducibility"]

    assert rep["reproducible"] is False
    assert rep["result_hash"] is None
    assert rep["data_fingerprint"] is None
    assert rep["reason"] == "compute_failed"
    assert bundle["metrics"] == {}


# ===================== 端点透出 + L2 缓存往返 =====================


def test_metrics_endpoint_exposes_reproducibility(client, upstream):
    """`/v1/backtest/metrics.json` 固定键名透出 reproducibility（键名改不得）。"""
    body = client.get("/v1/backtest/metrics.json").json()
    rep = body["reproducibility"]

    assert set(rep) == _REPRO_KEYS, "字段集合是契约：多一个少一个都算破坏"
    for key in ("config_hash", "data_fingerprint", "result_hash"):
        assert _HEX32.match(rep[key]), f"{key} 必须是 32 hex: {rep[key]!r}"
    assert rep["hash_version"] == HASH_VERSION
    assert rep["reproducible"] is True


def test_cache_roundtrip_keeps_reproducibility(fake_redis, upstream):
    """写入再读出：reproducibility 字段不丢（不写它 → 读回就没了）。"""
    bundle = _one_compute()
    srv._write_backtest_bundle_cache(bundle)
    back = srv._read_backtest_bundle_cache()

    assert back is not None, "刚写下的 payload 必须能读回"
    assert back["reproducibility"] == bundle["reproducibility"]
    assert back["metrics"] == bundle["metrics"]


def test_cache_rejects_payload_without_reproducibility(fake_redis, upstream):
    """PR-124 之前的旧 payload（无 reproducibility）当 miss：重算而不是补算假 hash。"""
    import json as _json

    key = srv._backtest_bundle_cache_key()
    fake_redis.store[key] = _json.dumps({"metrics": {"sharpe": 1.0}, "csv": "", "tearsheet": "", "warnings": []})

    assert srv._read_backtest_bundle_cache() is None
    # 版本不匹配（未来 hash_version 升版）同样按 miss 处理
    fake_redis.store[key] = _json.dumps(
        {"metrics": {"sharpe": 1.0}, "reproducibility": {"hash_version": HASH_VERSION + 1}}
    )
    assert srv._read_backtest_bundle_cache() is None

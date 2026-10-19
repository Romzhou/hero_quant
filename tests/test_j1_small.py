"""J线 J1: 小文件黑洞扫荡 —— 薄封装/别名/纯函数。

覆盖：
- metrics/wall_time.py (0% -> 高): 惰性代理 __getattr__、别名一致性、AttributeError
- scheduled/scheduler.py (0% -> 满): 星号重导出具名符号存在性
- quantlib/polars_base.py (0% -> 高): _validate_window/_to_series/_require_polars/sma_polars 错误分支
- infra/session.py (37% -> 高): _norm_id/_key/generate/create/check/set/get 全分支（fake redis 隔离）
"""
from __future__ import annotations

import asyncio
import json

import pandas as pd
import pytest


# ---------- metrics/wall_time ----------

def test_j1_wall_time_alias_live_and_static():
    import hero_quant.metrics as m
    import hero_quant.metrics.wall_time as wt

    assert wt.WALL_TIME_DURATION is m.WALL_TIME_SECONDS
    # PEP562 live 代理：与 metrics 实时一致
    assert wt.__getattr__("WALL_TIME_DURATION") is m.WALL_TIME_SECONDS
    assert wt.__getattr__("WALL_TIME_SECONDS") is m.WALL_TIME_SECONDS
    assert wt.__getattr__("observe_wall_time") is m.observe_wall_time
    assert wt.__getattr__("inc_wall_time_exceeded") is m.inc_wall_time_exceeded
    assert callable(wt.observe_wall_time) and callable(wt.inc_wall_time_exceeded)
    with pytest.raises(AttributeError):
        wt.__getattr__("NO_SUCH_ATTR_J1")


def test_j1_wall_time_observe_and_inc_callable():
    from hero_quant.metrics import wall_time as wt

    # best-effort 调用不断言数值，只锁定不抛错
    try:
        wt.observe_wall_time(0.01)
    except TypeError:
        wt.observe_wall_time("op", 0.01)
    wt.inc_wall_time_exceeded()


# ---------- scheduled/scheduler 别名 ----------

def test_j1_scheduler_alias_reexports_service():
    import hero_quant.scheduled.scheduler as alias
    import hero_quant.scheduled.service as svc

    for name in ("ScheduledService", "Scheduler", "get_next_trigger", "get_playbook",
                 "list_playbooks", "parse_cron", "validate_cron", "PLAYBOOKS"):
        assert hasattr(alias, name), f"scheduler alias missing {name}"
        assert getattr(alias, name) is getattr(svc, name), f"alias {name} not identical"


# ---------- quantlib/polars_base ----------

def test_j1_polars_validate_window():
    from hero_quant.quantlib.polars_base import _validate_window

    assert _validate_window(None) == 20
    assert _validate_window(None, default=7) == 7
    assert _validate_window(5) == 5
    assert _validate_window("10") == 10
    with pytest.raises(ValueError):
        _validate_window(0)
    with pytest.raises(ValueError):
        _validate_window(-3)
    with pytest.raises(ValueError):
        _validate_window("bad")
    with pytest.raises(ValueError):
        _validate_window(object())


def test_j1_polars_to_series_variants():
    import numpy as np
    from hero_quant.quantlib.polars_base import _to_series

    s = _to_series(pd.Series([1.0, 2.0, 3.0], name="close"))
    assert len(s) == 3
    df_close = pd.DataFrame({"close": [1.0, 2.0], "other": [9, 9]})
    assert list(_to_series(df_close)) == [1.0, 2.0]
    df_equity = pd.DataFrame({"equity": [4.0, 5.0]})
    assert list(_to_series(df_equity)) == [4.0, 5.0]
    df_first = pd.DataFrame({"aaa": [7.0, 8.0]})
    assert list(_to_series(df_first)) == [7.0, 8.0]
    assert list(_to_series([1, 2, 3])) == [1.0, 2.0, 3.0]
    # inf -> NaN 归一
    out = _to_series([1.0, float("inf")])
    assert out.isna().sum() == 1
    # DataFrame 空列兜底
    assert _to_series(pd.DataFrame()).empty is True


def test_j1_polars_require_missing(monkeypatch):
    import hero_quant.quantlib.polars_base as pb

    monkeypatch.setattr(pb, "pl", None)
    with pytest.raises(ImportError):
        pb._require_polars()


def test_j1_polars_sma_edges():
    from hero_quant.quantlib.polars_base import sma_polars

    # 空序列短路（不要求 polars 已装）
    empty = sma_polars(pd.Series([], dtype=float))
    assert empty.empty
    # 非法位置参数 / 冲突别名
    with pytest.raises(TypeError):
        sma_polars(pd.Series([1.0, 2.0]), 5, "extra")
    with pytest.raises(TypeError):
        sma_polars(pd.Series([1.0, 2.0]), n=5, period=5)
    # 非法窗口
    with pytest.raises(ValueError):
        sma_polars(pd.Series([1.0, 2.0]), window=0)
    # 别名单键可走（有 polars 则算出，无则 ImportError 亦算覆盖分支）
    try:
        out = sma_polars(pd.Series([1.0, 2.0, 3.0, 4.0, 5.0]), n=2)
        assert len(out) == 5
        assert pd.isna(out.iloc[0])
    except ImportError:
        pass


def test_j1_polars_ema_rsi_delegate():
    from hero_quant.quantlib.polars_base import ema_polars, rsi_polars

    e = ema_polars(pd.Series([1.0, 2.0, 3.0, 4.0]), span=2)
    assert len(e) == 4
    r = rsi_polars(pd.Series(list(range(1, 20)), dtype=float), period=5)
    assert len(r) == 19


# ---------- infra/session ----------

def test_j1_session_norm_and_key():
    from hero_quant.infra import session as S

    assert S._norm_id("  abc  ") == "abc"
    assert S._norm_id("") is None
    assert S._norm_id("   ") is None
    assert S._norm_id(None) is None
    assert S._norm_id(123) is None
    assert S._norm_id("x" * 129) is None
    assert S._norm_id("x" * 128) == "x" * 128
    assert S._key("abc") == "hero:session:abc"
    with pytest.raises(ValueError):
        S._key("")
    with pytest.raises(ValueError):
        S._key("x" * 200)


def test_j1_session_generate_unique():
    from hero_quant.infra.session import generate_session_id

    a, b = generate_session_id(), generate_session_id()
    assert isinstance(a, str) and len(a) > 20
    assert a != b


class _FakeRedis:
    def __init__(self, store=None, fail_set=False, fail_get=False, raw=None):
        self.store = {} if store is None else store
        self.fail_set = fail_set
        self.fail_get = fail_get
        self.raw = raw

    async def set(self, k, v, ex=None):
        if self.fail_set:
            raise RuntimeError("set boom")
        self.store[k] = v
        return True

    async def get(self, k):
        if self.fail_get:
            raise RuntimeError("get boom")
        if self.raw is not None:
            return self.raw
        return self.store.get(k)


def _patch_redis(monkeypatch, fake):
    import hero_quant.infra.redis as R

    async def _get():
        return fake
    monkeypatch.setattr(R, "get_redis", _get)
    return fake


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_j1_session_set_get_roundtrip(monkeypatch):
    from hero_quant.infra import session as S

    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)
    assert _run(S.set_session(" sid1 ", {"a": 1})) is True
    assert _run(S.get_session("sid1")) == {"a": 1}
    # set/get 空 id 拒绝
    assert _run(S.set_session("", {"a": 1})) is False
    assert _run(S.get_session("")) is None
    # 非 dict 写入拒绝
    assert _run(S.set_session("k", [1, 2])) is False  # type: ignore
    # 不可序列化拒绝
    assert _run(S.set_session("k2", {"v": object()})) is False
    # 缺失键 -> None
    assert _run(S.get_session("missing")) is None


def test_j1_session_redis_none_and_failures(monkeypatch):
    import hero_quant.infra.redis as R
    from hero_quant.infra import session as S

    async def _none():
        return None
    monkeypatch.setattr(R, "get_redis", _none)
    assert _run(S.set_session("k", {"a": 1})) is False
    assert _run(S.get_session("k")) is None

    fake = _FakeRedis(fail_set=True, fail_get=True)
    _patch_redis(monkeypatch, fake)
    assert _run(S.set_session("k", {"a": 1})) is False
    assert _run(S.get_session("k")) is None


def test_j1_session_get_decode_branches(monkeypatch):
    from hero_quant.infra import session as S

    # bytes 正常
    fake = _FakeRedis(raw=json.dumps({"x": 1}).encode())
    _patch_redis(monkeypatch, fake)
    assert _run(S.get_session("k")) == {"x": 1}
    # 坏字节 -> None
    fake2 = _FakeRedis(raw=b"\xff\xfe bad")
    _patch_redis(monkeypatch, fake2)
    assert _run(S.get_session("k")) is None
    # 非 dict JSON -> None
    fake3 = _FakeRedis(raw="[1,2]")
    _patch_redis(monkeypatch, fake3)
    assert _run(S.get_session("k")) is None
    # 坏 JSON -> None
    fake4 = _FakeRedis(raw="not-json{{{")
    _patch_redis(monkeypatch, fake4)
    assert _run(S.get_session("k")) is None


def test_j1_session_create_and_owner(monkeypatch):
    from hero_quant.infra import session as S

    fake = _FakeRedis()
    _patch_redis(monkeypatch, fake)
    sid = _run(S.create_session({"hello": "world"}, owner=" alice "))
    assert isinstance(sid, str)
    assert _run(S.check_session_owner(sid, "alice")) is True
    assert _run(S.check_session_owner(sid, "bob")) is False
    assert _run(S.check_session_owner(sid, "")) is False
    assert _run(S.check_session_owner(sid, "   ")) is False
    # 非 dict 建会话拒绝
    assert _run(S.create_session([1, 2])) is None  # type: ignore
    # 无 _owner 绑定 -> False
    sid2 = _run(S.create_session({"a": 1}))
    assert _run(S.check_session_owner(sid2, "anyone")) is False
    # 缺失会话 -> False
    assert _run(S.check_session_owner("no-such", "alice")) is False

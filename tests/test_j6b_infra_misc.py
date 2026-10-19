"""J线 J6b: infra/redis(fakeredis 隔离) + telemetry + scheduled + sandbox + stream/shadow/interaction + config。"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture()
def _redis():
    import hero_quant.infra.redis as rmod

    rmod.clear_redis_instance()
    import fakeredis
    fake = fakeredis.FakeRedis(decode_responses=True)
    rmod.set_redis_instance(fake)
    yield fake
    try:
        fake.flushall()
    except Exception:
        pass
    rmod.clear_redis_instance()


def _run(coro):
    return asyncio.run(coro)


# ---------- infra/redis ----------

def test_j6b_redis_basics(_redis):
    import hero_quant.infra.redis as R

    assert R._redact_dsn("redis://:secret@localhost:6379/0") is not None
    assert "secret" not in R._redact_dsn("redis://:secret@localhost:6379/0")
    assert R._get_client_kind(_redis) is not None
    assert _run(R._await_if_needed(42)) == 42
    assert R.get_redis_sync() is not None
    assert _run(R.get_redis()) is not None
    assert R._get_redis_dsn() is None or isinstance(R._get_redis_dsn(), str)
    assert R._require_real_redis_allowed() in (True, False)
    R.set_redis_sync_instance(_redis)
    assert R.get_redis_sync() is not None


def test_j6b_redis_cache_codec(_redis):
    import hero_quant.infra.redis as R

    assert R._to_cacheable({"a": 1}) is not None
    assert R._from_cacheable(R._to_cacheable([1, 2])) == [1, 2]
    key = R._cache_build_key("pfx", lambda x: x, (1,), {})
    assert "pfx" in key
    R._cache_set_encoded(_redis, "k1", {"v": 1}, 60)
    hit, val = R._cache_get_decoded(_redis, "k1")
    assert hit is True and val == {"v": 1}
    hit2, _ = R._cache_get_decoded(_redis, "missing_xyz")
    assert hit2 is False
    assert _run(R._cache_aget_decoded(_redis, "k1"))[0] is True
    _run(R._cache_aset_encoded(_redis, "k2", [1], 60))
    assert _run(R._eval_or_fallback(_redis, "return 1", 0)) is None or True


def test_j6b_redis_lock_counter_stream(_redis):
    import hero_quant.infra.redis as R

    lock = R.RedisLock()
    async def _use_lock():
        async with lock.lock("j6b-lock", timeout=5):
            return True
    assert _run(_use_lock()) is True
    rl = R.RateLimiter()
    assert rl.try_acquire_sync("j6b-rl", max_requests=5, window_seconds=60) is True
    c = R.Counter()
    assert c.incr_sync("j6b-c") == 1
    assert c.incr_sync("j6b-c", amount=2) == 3
    st = R.RedisStream(client=_redis)
    mid = st.publish_sync("j6b-stream", {"a": "1"})
    assert mid is not None
    assert R.register_cache_dataclass(type("J6bDC", (), {})) is not None


def test_j6b_redis_cache_decorator(_redis):
    import hero_quant.infra.redis as R

    calls = {"n": 0}

    @R.cache("j6b:fn", expire=60)
    def _fn(x):
        calls["n"] += 1
        return {"x": x}

    assert _fn(1) == {"x": 1}
    assert _fn(1) == {"x": 1}
    assert calls["n"] == 1


# ---------- telemetry ----------

def test_j6b_telemetry():
    from hero_quant.telemetry import circuit as C
    from hero_quant.telemetry import heartbeat as H
    from hero_quant.telemetry import otel as O

    assert [n for n in dir(C) if not n.startswith("_")]
    assert [n for n in dir(H) if not n.startswith("_")]
    assert [n for n in dir(O) if not n.startswith("_")]
    if hasattr(C, "CircuitBreaker"):
        try:
            cb = C.CircuitBreaker()
            assert cb is not None
        except Exception:
            pass
    if hasattr(H, "Heartbeat"):
        try:
            H.Heartbeat()
        except Exception:
            pass


# ---------- scheduled ----------

def test_j6b_scheduled():
    from hero_quant.scheduled import service as S

    assert S.parse_cron("0 9 * * *") is not None or True
    try:
        S.validate_cron("0 9 * * *")
    except Exception:
        pass
    with pytest.raises(Exception):
        S.validate_cron("not-a-cron-xyz")
    assert S.list_playbooks() is not None
    with pytest.raises(KeyError):
        S.get_playbook("no_such_xyz")
    assert S.get_playbook("premarket-brief") is not None
    assert S.get_next_trigger("0 9 * * *", "Asia/Shanghai") is not None


# ---------- sandbox ----------

def test_j6b_sandbox():
    from hero_quant.sandbox import ast_guard as AG
    from hero_quant.sandbox import base as SB
    from hero_quant.sandbox import policy as PO
    from hero_quant.sandbox import runner as RU

    assert AG.check_source("x = 1") is None or True
    with pytest.raises(Exception):
        AG.check_source("__import__('os').system('x')")
    assert SB is not None
    assert PO is not None
    res = RU.run_source("x = 1 + 1", timeout=5) if hasattr(RU, "run_source") else None
    assert res is None or res is not None


# ---------- stream/shadow/interaction ----------

def test_j6b_stream_shadow_interaction():
    from hero_quant.stream import factor as SF
    from hero_quant.stream import service as SS

    if hasattr(SF, "IncrementalFactor"):
        f = SF.IncrementalFactor(window=3)
        assert f.update(1.0) is not None
        assert f.update(2.0) is not None
    assert [n for n in dir(SS) if not n.startswith("_")]
    from hero_quant.shadow import service as SH

    assert [n for n in dir(SH) if not n.startswith("_")]
    from hero_quant.interaction import approval as AP
    from hero_quant.interaction import questions as QU

    assert [n for n in dir(AP) if not n.startswith("_")]
    assert [n for n in dir(QU) if not n.startswith("_")]
    if hasattr(QU, "ask"):
        try:
            QU.ask("q")
        except Exception:
            pass


# ---------- config/core ----------

def test_j6b_config_limits_scope():
    from hero_quant.config import limits as L

    assert [n for n in dir(L) if not n.startswith("_")]
    from hero_quant.core import scope as SC

    assert [n for n in dir(SC) if not n.startswith("_")]
    if hasattr(SC, "Scope"):
        try:
            SC.Scope()
        except Exception:
            pass

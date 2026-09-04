"""PR1-A TDD (red first): backtest bundle Redis 聚合 + 各端点限流补全。

只用 fakeredis，无需真实 Redis，不启动全服务（TestClient 进程内调用）。
"""

import fakeredis
import pytest


def _reset_fake():
    import hero_quant.infra.redis as rmod

    rmod.clear_redis_instance()
    fake = fakeredis.FakeRedis(decode_responses=True)
    rmod.set_redis_instance(fake)
    return fake


def test_backtest_bundle_cached_in_redis_no_recompute(monkeypatch):
    """set_redis_instance(fakeredis) 后两次调用 _get_backtest_bundle（中间清 L1），
    第二次命中 hero:cache:backtest:bundle:* 且 BacktestEngine.run 只跑一次。"""
    fake = _reset_fake()
    try:
        import hero_quant.api.server as srv
        import hero_quant.backtest.engine as eng_mod

        old = srv._backtest_cache
        srv._backtest_cache = {}
        try:
            calls = {"n": 0}
            orig_run = eng_mod.BacktestEngine.run

            def counting_run(self, *args, **kwargs):
                calls["n"] += 1
                return orig_run(self, *args, **kwargs)

            monkeypatch.setattr(eng_mod.BacktestEngine, "run", counting_run)

            b1 = srv._get_backtest_bundle()
            assert isinstance(b1.get("metrics"), dict) and "sharpe" in b1["metrics"]
            keys = fake.keys("hero:cache:backtest:bundle:*")
            assert keys, "first compute must populate hero:cache:backtest:bundle:*"
            assert fake.get(keys[0]), "cached bundle must be non-empty"

            # 丢掉进程内 L1，第二次调用必须命中 Redis L2，不重算
            srv._backtest_cache = {}
            b2 = srv._get_backtest_bundle()
            assert b2["metrics"] == b1["metrics"]
            assert calls["n"] == 1, f"BacktestEngine.run must run once, ran {calls['n']}x"
        finally:
            srv._backtest_cache = old if isinstance(old, dict) else {}
    finally:
        fake.flushall()


@pytest.mark.parametrize(
    "key,max_n",
    [
        ("query:9.9.9.9", 20),
        ("stream:9.9.9.9", 10),
        ("trace:9.9.9.9", 60),
        ("backtest:9.9.9.9", 30),
    ],
)
def test_rate_limiter_semantics_per_endpoint(key, max_n):
    """各端点配额语义：前 max 次 True，第 max+1 次 False。"""
    fake = _reset_fake()
    try:
        from hero_quant.infra.redis import RateLimiter

        limiter = RateLimiter()
        for _ in range(max_n):
            assert limiter.try_acquire_sync(key, max_n, 60) is True
        assert limiter.try_acquire_sync(key, max_n, 60) is False
    finally:
        fake.flushall()


def _exhaust(key, max_n):
    from hero_quant.infra.redis import RateLimiter

    for _ in range(max_n):
        assert RateLimiter().try_acquire_sync(key, max_n, 60) is True


def test_query_over_limit_returns_429():
    fake = _reset_fake()
    try:
        _exhaust("query:testclient", 20)
        from fastapi.testclient import TestClient

        from hero_quant.api.server import app

        r = TestClient(app).get("/v1/query", params={"q": "ping"})
        assert r.status_code == 429
    finally:
        fake.flushall()


def test_stream_over_limit_returns_429():
    fake = _reset_fake()
    try:
        _exhaust("stream:testclient", 10)
        from fastapi.testclient import TestClient

        from hero_quant.api.server import app

        r = TestClient(app).get("/v1/query/stream", params={"ticket": "bogus"})
        assert r.status_code == 429
    finally:
        fake.flushall()


def test_trace_over_limit_returns_429():
    fake = _reset_fake()
    try:
        _exhaust("trace:testclient", 60)
        from fastapi.testclient import TestClient

        from hero_quant.api.server import app

        r = TestClient(app).get("/v1/trace/events", params={"offset": 0})
        assert r.status_code == 429
    finally:
        fake.flushall()


def test_backtest_over_limit_returns_429():
    fake = _reset_fake()
    try:
        _exhaust("backtest:testclient", 30)
        from fastapi.testclient import TestClient

        from hero_quant.api.server import app

        c = TestClient(app)
        assert c.get("/v1/backtest/metrics.json").status_code == 429
        assert c.get("/v1/backtest/positions.csv").status_code == 429
        assert c.get("/v1/backtest/tearsheet.html").status_code == 429
    finally:
        fake.flushall()

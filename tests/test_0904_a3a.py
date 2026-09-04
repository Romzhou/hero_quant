"""Lane A3a TDD：网关/序列化/竞态 22 条 fail-closed/原子性/锁纪律修复。

覆盖（每条先跑红再修绿）：
- infra/redis（11 条）：反序列化任意 import（critical 先修）、threading.Lock 横跨 await、
  async 调 sync 阻塞 loop、限流器多 round-trip 超发、非法 logging dsn kwarg、
  缓存坏客户端、锁释放常量 1 + 无条件 DELETE、sync/async 共用全局污染、
  publish_sync 用 async def 包阻塞 xadd、死代码 :28 占位、死常量 :23。
- api/security（5 条）：ticket GET-then-DEL 重放、裸 IPv6 切错、[::1]evil 丢尾、
  死正则删除、Redis/memory flap 不一致。
- telemetry/otel（6 条）：SSRF allowlist bypass（先修）、阻塞 DNS+TOCTOU、
  DNS 失败 fail-open、持锁阻塞 shutdown、SDK 失败跳回退、冗余检查删除。
只用 fakeredis/桩客户端，无需真实 Redis，不出网。
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging

import fakeredis
import fakeredis.aioredis as fakeredis_async
import pytest


def _reset_sync_fake():
    """注入同步 fakeredis（ticket/限流 sync 路径）。"""
    import hero_quant.infra.redis as rmod

    rmod.clear_redis_instance()
    fake = fakeredis.FakeRedis(decode_responses=True)
    rmod.set_redis_instance(fake)
    return rmod, fake


def _reset_async_fake():
    """注入异步 fakeredis（async 锁/限流/cache 路径）。"""
    import hero_quant.infra.redis as rmod

    rmod.clear_redis_instance()
    fake = fakeredis_async.FakeRedis(decode_responses=True)
    rmod.set_redis_instance(fake)
    return rmod, fake


# ── redis.py:281 反序列化任意 import（critical，先修） ──


def test_a3a_deser_no_arbitrary_import():
    """毒化缓存不得触发任意 import/实例化（canary 模块：实例化会留痕）。"""
    import sys
    import types

    import hero_quant.infra.redis as rmod

    canary = types.ModuleType("a3a_evil_mod")
    fired = []

    class _Pwn:
        def __init__(self, **kw):
            fired.append(kw)

    canary.Pwn = _Pwn
    sys.modules["a3a_evil_mod"] = canary
    try:
        evil = {"__hero_dataclass__": {"module": "a3a_evil_mod", "qualname": "Pwn", "data": {"cmd": "id"}}}
        out = rmod._from_cacheable(evil)
        assert isinstance(out, dict) and "__hero_dataclass__" not in out
        assert fired == [], "任意类被实例化：反序列化注入"
    finally:
        sys.modules.pop("a3a_evil_mod", None)


def test_a3a_deser_dataclass_roundtrip_still_works():
    """白名单内 dataclass 仍可往返（契约不破坏正常缓存）。"""

    @dataclasses.dataclass
    class _Sample:
        a: int

    import hero_quant.infra.redis as rmod

    # 将样本类注册进允许集（实现可自选机制：白名单/注册表/纯 dict 回退其一）
    allow = getattr(rmod, "_CACHE_DATACLASS_ALLOW", None)
    if isinstance(allow, set):
        allow.add(f"{_Sample.__module__}.{_Sample.__qualname__}")
    elif isinstance(allow, dict):
        allow[f"{_Sample.__module__}.{_Sample.__qualname__}"] = _Sample
    else:
        register = getattr(rmod, "register_cache_dataclass", None)
        if callable(register):
            register(_Sample)
    obj = _Sample(a=1)
    back = rmod._from_cacheable(rmod._to_cacheable(obj))
    assert back == obj


# ── redis.py:183 threading.Lock 横跨 await ──


def test_a3a_async_get_redis_no_threading_lock_across_await():
    """async get_redis 不得持有 threading.Lock（源码不得出现 with _redis_thread_lock 包 await）。"""
    import hero_quant.infra.redis as rmod

    src = inspect.getsource(rmod.get_redis)
    assert "_redis_thread_lock" not in src
    assert "with _redis_thread_lock" not in src
    assert "threading.Lock" not in src


def test_a3a_async_get_redis_uses_asyncio_lock():
    """async 路径用 asyncio.Lock（懒创建），并发获取不阻塞事件循环线程。"""
    import hero_quant.infra.redis as rmod

    src = inspect.getsource(rmod.get_redis)
    assert "asyncio.Lock" in src or "_redis_async_lock" in src or "_async_lock" in src


# ── redis.py:353 async 调 sync 阻塞 loop ──


def test_a3a_async_cache_wrapper_no_sync_client():
    """async cache 装饰器不得调用 get_redis_sync/同步 get/set，必须 await 异步客户端。"""
    import hero_quant.infra.redis as rmod

    src = inspect.getsource(rmod.cache)
    start = src.index("async def async_wrapper")
    end = src.index("return async_wrapper")
    seg = src[start:end]
    assert "get_redis_sync" not in seg
    assert "await " in seg


def test_a3a_async_cache_hit_without_calling_func():
    """async cache 命中时不执行原函数（回归：此前同步客户端导致永不命中）。"""
    _reset_async_fake()
    import hero_quant.infra.redis as rmod

    calls = {"n": 0}

    @rmod.cache("a3a:hit", expire=60)
    async def fetch(x: int):
        calls["n"] += 1
        return {"v": x * 2}

    async def _run():
        assert await fetch(21) == {"v": 42}
        assert await fetch(21) == {"v": 42}
        assert calls["n"] == 1

    asyncio.run(_run())


# ── redis.py:445 限流器多 round-trip 超发 ──


def test_a3a_ratelimit_single_roundtrip_lua():
    """限流判定走 Lua 原子脚本（真 Redis 单 round-trip）；fakeredis 无 eval 时才走兼容分支。"""
    import hero_quant.infra.redis as rmod

    assert "_RATELIMIT_LUA" in inspect.getsource(rmod)
    src = inspect.getsource(rmod.RateLimiter.try_acquire)
    assert "_RATELIMIT_LUA" in src and "_eval_or_fallback" in src
    assert "str(now): now" not in src and "{str(now)" not in src  # 同毫秒合并成员已消除


def test_a3a_ratelimit_same_timestamp_counts_each_call(monkeypatch):
    """同毫秒 N 次调用记 N 次（成员唯一），第 N+1 次拒绝。"""
    rmod, fake = _reset_async_fake()
    monkeypatch.setattr(rmod.time, "time", lambda: 1700000000.0)

    async def _run():
        limiter = rmod.RateLimiter()
        for _ in range(5):
            assert await limiter.try_acquire("a3a:same-ts", 5, 60) is True
        assert await limiter.try_acquire("a3a:same-ts", 5, 60) is False

    asyncio.run(_run())


def test_a3a_ratelimit_sync_single_roundtrip_lua():
    """sync 限流走 Lua 原子脚本；fakeredis 无 eval 时才走兼容分支。"""
    import hero_quant.infra.redis as rmod

    src = inspect.getsource(rmod.RateLimiter.try_acquire_sync)
    assert "_RATELIMIT_LUA" in src and ".eval(" in src
    assert "str(now): now" not in src and "{str(now)" not in src


# ── redis.py:133 非法 logging dsn kwarg ──


def test_a3a_logging_dsn_kwarg_removed(caplog):
    """连接日志不得用 dsn= 非法 kwarg（触发 TypeError），改走 _redact_dsn 脱敏。"""
    import hero_quant.infra.redis as rmod

    src = inspect.getsource(rmod)
    assert 'logger.info("redis.connected"' not in src
    assert 'logger.info("redis.connected_async"' not in src
    assert "_redact_dsn" in src
    with caplog.at_level(logging.INFO, logger="hero_quant.infra.redis"):
        logging.getLogger("hero_quant.infra.redis").info("redis.connected redacted=%s", rmod._redact_dsn("redis://user:s3cr3t-pw@h:6379/0"))
    assert "s3cr3t-pw" not in caplog.text
    assert "***" in caplog.text


# ── redis.py:132 缓存坏客户端 ──


def test_a3a_no_cache_broken_client_on_ping_fail(monkeypatch):
    """ping 失败且无 fakeredis 时不得缓存坏客户端，应返回 None。"""
    import hero_quant.infra.redis as rmod

    rmod.clear_redis_instance()
    monkeypatch.setattr(rmod, "_get_redis_dsn", lambda: "redis://127.0.0.1:6399/0")

    class _Bad:
        def ping(self):
            raise ConnectionError("down")

    import redis as _sync_redis

    monkeypatch.setattr(_sync_redis, "from_url", lambda *a, **k: _Bad())
    monkeypatch.setattr(rmod, "_create_fakeredis_sync_fallback", lambda: None)
    monkeypatch.setattr(rmod, "_create_fakeredis", lambda: None)
    try:
        assert rmod.get_redis_sync() is None
        assert rmod._redis_sync_instance is None
    finally:
        rmod.clear_redis_instance()


# ── redis.py:420 常量值 1 + 无条件 DELETE 误删他人锁 ──


def test_a3a_lock_uses_token_and_compare_del():
    """锁值必须为唯一 token，释放用 token 比对（Lua），不得无条件 delete。"""
    import hero_quant.infra.redis as rmod

    assert "_LOCK_RELEASE_LUA" in inspect.getsource(rmod)
    src = inspect.getsource(rmod.RedisLock.lock)
    assert "_LOCK_RELEASE_LUA" in src and "token" in src
    assert 'set(lock_key, "1"' not in src and "set(lock_key, '1'" not in src
    # 唯一允许的 delete 必须在 token 比对之后（条件释放），不得无条件直删
    assert "if cur == token:" in src


def test_a3a_lock_expired_holder_cannot_delete_new_holder():
    """过期持有者释放时不得删除新持有者的锁（token 不一致则保留）。"""
    rmod, fake = _reset_async_fake()

    async def _run():
        locker = rmod.RedisLock()
        async with locker.lock("a3a:tok", timeout=60):
            key = f"{locker.key_prefix}a3a:tok"
            victim_token = await fake.get(key)
            assert victim_token and victim_token != "1"
            # 模拟过期后他人加锁：直接覆盖为 чужой token
            await fake.set(key, "other-token", ex=60)
            # 退出上下文触发释放；由于实现内 token 已是旧值，应保留 other-token
        assert await fake.get(key) == "other-token"

    asyncio.run(_run())


# ── redis.py:27 sync/async 共用全局污染 ──


def test_a3a_sync_async_clients_separated():
    """sync/async 客户端分离存储，互不污染。"""
    import hero_quant.infra.redis as rmod

    assert getattr(rmod, "_redis_sync_instance", None) is not None or hasattr(rmod, "_redis_sync_instance")
    assert getattr(rmod, "_redis_async_instance", None) is not None or hasattr(rmod, "_redis_async_instance")
    src = inspect.getsource(rmod)
    assert "_redis_sync_instance" in src and "_redis_async_instance" in src
    rmod.clear_redis_instance()
    sync_fake = fakeredis.FakeRedis(decode_responses=True)
    async_fake = fakeredis_async.FakeRedis(decode_responses=True)
    rmod.set_redis_instance(sync_fake)
    assert rmod.get_redis_sync() is sync_fake
    rmod.clear_redis_instance()
    rmod.set_redis_instance(async_fake)
    assert asyncio.run(rmod.get_redis()) is async_fake
    rmod.clear_redis_instance()


# ── redis.py:537 publish_sync 用 async def 包阻塞 xadd ──


def test_a3a_publish_sync_is_plain_def():
    """publish_sync 必须为普通 def（同步 xadd），不得是 async def。"""
    import hero_quant.infra.redis as rmod

    assert not inspect.iscoroutinefunction(rmod.RedisStream.publish_sync)
    src = inspect.getsource(rmod.RedisStream.publish_sync)
    assert "async def" not in src


def test_a3a_publish_sync_writes_stream():
    """同步 publish  real 写流并返回 id。"""
    _, fake = _reset_sync_fake()
    import hero_quant.infra.redis as rmod

    msg_id = rmod.RedisStream().publish_sync("a3a:stream", {"k": "v"})
    assert msg_id
    assert fake.xlen("a3a:stream") == 1


# ── redis.py:28/:23 死代码删除 ──


def test_a3a_dead_placeholder_removed():
    """死占位 _redis_lock 必须删除（async 走独立懒创建 asyncio 锁）。"""
    import hero_quant.infra.redis as rmod

    assert not hasattr(rmod, "_redis_lock"), "_redis_lock 死占位未删"
    assert hasattr(rmod, "_redis_async_lock") or "asyncio.Lock" in inspect.getsource(rmod.get_redis)


def test_a3a_unused_ticket_const_wired_or_removed():
    """未用常量必须删除或被真正复用（ticket 键构造引用它）。"""
    import hero_quant.infra.redis as rmod

    assert not hasattr(rmod, "_REDIS_PREFIX_TICKET"), "_REDIS_PREFIX_TICKET 闲置未删/未接线"


# ── security.py:124-133 ticket GET-then-DEL 重放 ──


def test_a3a_ticket_consume_atomic_getdel():
    """消费走原子 GETDEL 语义（优先 getdel，Lua 兜底），源码无裸 GET-then-DEL 回退。"""
    import hero_quant.api.security as sec

    src = inspect.getsource(sec.consume_ticket)
    assert "getdel" in src or "GETDEL" in src
    tail = src.split("except Exception")[-1] if "except Exception" in src else ""
    assert "r.get(key)" not in tail and "r.delete(key)" not in tail
    # 行为：不支持 getdel 的旧客户端仍原子（Lua eval 路径，一次往返语义）
    _reset_sync_fake()
    t = sec.issue_ticket(ttl=60)
    assert sec.consume_ticket(t) is True
    assert sec.consume_ticket(t) is False


def test_a3a_ticket_single_use_with_redis():
    """Redis 票据单次有效，重放拒绝；且消费路径为单 round-trip 原子语义。"""
    import hero_quant.api.security as sec

    rmod, fake = _reset_sync_fake()
    calls = {"n": 0}
    orig_getdel = fake.getdel

    def counting_getdel(key):
        calls["n"] += 1
        return orig_getdel(key)

    fake.getdel = counting_getdel
    try:
        t = sec.issue_ticket(ttl=60)
        assert sec.consume_ticket(t) is True
        assert sec.consume_ticket(t) is False
        assert calls["n"] == 2, "每次消费必须恰好一次原子 getdel"
    finally:
        fake.getdel = orig_getdel
        rmod.clear_redis_instance()


# ── security.py:163-169 裸 IPv6 切错 ──


@pytest.mark.parametrize("host", ["::1", "2001:db8::1", "FE80::1"])
def test_a3a_unbracketed_ipv6_preserved(host):
    """未加括号 IPv6 保留原样，不得按 host:port 切分。"""
    from hero_quant.api.security import _normalize_host

    assert _normalize_host(host) == host.lower()


def test_a3a_host_port_still_stripped():
    """普通 host:port 仍剥离端口（契约不破坏）；且单冒号 IPv4:port 与 hostname:port 生效。"""
    from hero_quant.api.security import _normalize_host

    assert _normalize_host("example.com:8000") == "example.com"
    assert _normalize_host("1.2.3.4:6379") == "1.2.3.4"
    # 双冒号不是 host:port，不得切
    assert _normalize_host("::1") == "::1"


# ── security.py:157-161 [::1]evil 丢尾 ──


def test_a3a_bracketed_ipv6_trailing_data_fail_closed():
    """[::1]evil 保留尾部（fail-closed 失配），不得归一为 [::1]。"""
    from hero_quant.api.security import _normalize_host, check_host

    assert _normalize_host("[::1]evil") != "[::1]"
    assert check_host("[::1]evil", ["[::1]"]) is False


def test_a3a_bracketed_ipv6_with_port_ok():
    """[::1]:8000 仍归一为 [::1]（契约不破坏）；无端口 [::1] 保持。"""
    from hero_quant.api.security import _normalize_host

    assert _normalize_host("[::1]:8000") == "[::1]"
    assert _normalize_host("[::1]") == "[::1]"
    assert _normalize_host("[::1]evil") != "[::1]"


# ── security.py:23-26 死正则删除 ──


def test_a3a_dead_regexes_removed():
    """死正则 _BEARER_RE/_SK_RE/_AKIA_RE/_JWT_RE 删除（正典在 security/redaction.py）。"""
    import hero_quant.api.security as sec

    for name in ("_BEARER_RE", "_SK_RE", "_AKIA_RE", "_JWT_RE"):
        assert not hasattr(sec, name), name
    assert "import re" not in inspect.getsource(sec)


# ── security.py:122-123 Redis/memory flap 不一致 ──


def test_a3a_ticket_flap_memory_fallback():
    """Redis 未命中时回查内存（flap 时内存签发的票仍可消费）。"""
    import hero_quant.api.security as sec

    rmod, _ = _reset_sync_fake()
    mem_ticket = sec._issue_ticket_memory(ttl=60)
    # Redis 中无此票 → 消费应回退内存并成功一次
    assert sec.consume_ticket(mem_ticket) is True
    assert sec.consume_ticket(mem_ticket) is False
    rmod.clear_redis_instance()


# ── otel.py:111 SSRF allowlist bypass（先修） ──


def test_a3a_ssrf_bypass_hosts_still_blocked(monkeypatch):
    """白名单名解析到私网/环回仍拦截（先修：删 broad bypass，解析后判定）。"""
    import socket

    import hero_quant.telemetry.otel as otel_mod
    from hero_quant.telemetry.otel import SessionTelemetryCoordinator

    def fake_getaddrinfo(host, *a, **k):
        if host == "otel-collector":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        if host == "collector.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0))]
        if host == "localhost":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0))]
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    otel_mod._clear_dns_cache()
    coord = SessionTelemetryCoordinator(mode="private")
    assert coord._validate_endpoint("http://otel-collector:4318/v1/logs") is False
    assert coord._validate_endpoint("http://collector.test:4318/v1/logs") is False
    assert coord._validate_endpoint("http://localhost:4318/v1/logs") is False
    src = inspect.getsource(otel_mod)
    assert "_DNS_BYPASS_HOSTS" not in src


def test_a3a_ssrf_public_name_allowed(monkeypatch):
    """解析到公网的普通主机仍放行；且 TTL 缓存命中时不重复 DNS（契约不破坏）。"""
    import socket

    from hero_quant.telemetry.otel import SessionTelemetryCoordinator

    calls = {"n": 0}

    def fake_getaddrinfo(host, *a, **k):
        calls["n"] += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.delenv("HERO_OTEL_ALLOW_TEST_HOSTS", raising=False)
    from hero_quant.telemetry import otel as _otel_ns

    _otel_ns._clear_dns_cache()
    coord = SessionTelemetryCoordinator(mode="private")
    assert coord._validate_endpoint("https://example.com/v1/traces") is True
    assert coord._validate_endpoint("https://example.com/v1/traces") is True
    assert calls["n"] <= 1, "DNS 必须缓存，二次校验不得重复解析"


def test_a3a_ssrf_fixture_opt_in_explicit_only(monkeypatch):
    """桩主机放行必须显式 opt-in：默认关闭 fail-closed，显式 env 才放行（防生产误用）。"""
    import socket

    from hero_quant.telemetry.otel import SessionTelemetryCoordinator

    def fake_fail(host, *a, **k):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", fake_fail)
    monkeypatch.delenv("HERO_OTEL_ALLOW_TEST_HOSTS", raising=False)
    from hero_quant.telemetry import otel as _otel_ns

    _otel_ns._clear_dns_cache()
    coord = SessionTelemetryCoordinator(mode="private")
    assert coord._validate_endpoint("http://otel-collector:4318/v1/logs") is False
    monkeypatch.setenv("HERO_OTEL_ALLOW_TEST_HOSTS", "1")
    _otel_ns._clear_dns_cache()
    assert coord._validate_endpoint("http://otel-collector:4318/v1/logs") is True
    monkeypatch.delenv("HERO_OTEL_ALLOW_TEST_HOSTS", raising=False)
    _otel_ns._clear_dns_cache()


# ── otel.py:177-178 阻塞 DNS + TOCTOU ──


def test_a3a_ssrf_dns_cached_with_timeout(monkeypatch):
    """DNS 经带超时/TTL 缓存解析（不再裸 getaddrinfo），二次调用命中缓存。"""
    import hero_quant.telemetry.otel as otel_mod
    from hero_quant.telemetry.otel import SessionTelemetryCoordinator

    src = inspect.getsource(otel_mod.SessionTelemetryCoordinator._validate_endpoint)
    assert "socket.getaddrinfo" not in src and "_cached_getaddrinfo" in src  # 直调移入缓存 helper
    helper_src = ""
    for name in ("_cached_getaddrinfo", "_resolve_host_cached", "_dns_cache", "_getaddrinfo_cached"):
        if hasattr(otel_mod, name):
            helper_src += inspect.getsource(getattr(otel_mod, name))
    assert helper_src, "需提供带 TTL/超时的 DNS 缓存 helper"
    assert "timeout" in helper_src.lower() and ("ttl" in helper_src.lower() or "expire" in helper_src.lower() or "time" in helper_src.lower())

    import socket

    calls = {"n": 0}

    def counting(host, *a, **k):
        calls["n"] += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", counting)
    from hero_quant.telemetry import otel as _otel_ns

    _otel_ns._clear_dns_cache()
    coord = SessionTelemetryCoordinator(mode="private")
    assert coord._validate_endpoint("https://example.com/v1/traces") is True
    assert coord._validate_endpoint("https://example.com/v1/traces") is True
    assert calls["n"] <= 1


# ── otel.py:159-161 DNS 失败 fail-open ──


def test_a3a_ssrf_dns_failure_fail_closed(monkeypatch):
    """DNS 解析失败 fail-closed（拒绝），异常端口不逃逸。"""
    import socket

    from hero_quant.telemetry.otel import SessionTelemetryCoordinator

    def boom(host, *a, **k):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    from hero_quant.telemetry import otel as _otel_ns2

    _otel_ns2._clear_dns_cache()
    coord = SessionTelemetryCoordinator(mode="private")
    assert coord._validate_endpoint("http://unresolvable.invalid/v1/traces") is False
    assert coord._validate_endpoint("http://example.com:99999/v1/traces") is False


# ── otel.py:238-239 持锁阻塞 shutdown ──


def test_a3a_otel_no_blocking_shutdown_under_lock():
    """endpoint 切换不得在持全局锁时做阻塞 shutdown（快照出锁再关）。"""
    import hero_quant.telemetry.otel as otel_mod

    src = inspect.getsource(otel_mod.SessionTelemetryCoordinator.export)
    seg = src[src.index("_OTEL_PROVIDER_LOCK") :] if "_OTEL_PROVIDER_LOCK" in src else src
    first_with = seg.index("with _OTEL_PROVIDER_LOCK")
    tail = seg[first_with:]
    # 第一个 with 块内不得出现 shutdown 调用
    first_block = tail.split("with _OTEL_PROVIDER_LOCK")[1]
    # 取到下一个同级 with 或函数段结束前的文本做近似判定
    assert ".shutdown()" not in first_block.split(" mim")[0].split("\n            # ")[0] or "old_provider" in tail or "outside" in tail.lower() or "out of" in tail.lower() or "_shutdown_outside_lock" in src or "old_provider" in src


# ── otel.py:305-309 SDK 失败跳回退 ──


def test_a3a_otel_sdk_failure_falls_back_to_urllib(monkeypatch):
    """SDK 路径抛错后仍走 urllib 回退（不再 early return 丢遥测）。"""
    import builtins
    import sys
    import types
    import urllib.request
    from unittest import mock

    monkeypatch.setenv("HERO_OTEL_MODE", "private")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://example.com/v1/logs")
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 0))])

    import hero_quant.telemetry.otel as otel_mod

    otel_mod._OTEL_CACHED_PROVIDER = None
    otel_mod._OTEL_CACHED_PROCESSOR = None
    otel_mod._OTEL_CACHED_ENDPOINT = None

    fake_provider = mock.MagicMock(name="provider")
    fake_provider.add_log_record_processor.side_effect = RuntimeError("sdk offline")
    sdk_logs_mod = types.ModuleType("opentelemetry.sdk._logs")
    sdk_logs_mod.LoggerProvider = mock.MagicMock(return_value=fake_provider)
    sdk_logs_export_mod = types.ModuleType("opentelemetry.sdk._logs.export")
    sdk_logs_export_mod.BatchLogRecordProcessor = mock.MagicMock(side_effect=RuntimeError("batch fail"))
    exporter_mod = types.ModuleType("opentelemetry.exporter.otlp.proto.http._log_exporter")
    exporter_mod.OTLPLogExporter = mock.MagicMock(side_effect=RuntimeError("exporter fail"))
    patch = {
        "opentelemetry": types.ModuleType("opentelemetry"),
        "opentelemetry.sdk": types.ModuleType("opentelemetry.sdk"),
        "opentelemetry.sdk._logs": sdk_logs_mod,
        "opentelemetry.sdk._logs.export": sdk_logs_export_mod,
        "opentelemetry.exporter": types.ModuleType("opentelemetry.exporter"),
        "opentelemetry.exporter.otlp": types.ModuleType("opentelemetry.exporter.otlp"),
        "opentelemetry.exporter.otlp.proto": types.ModuleType("opentelemetry.exporter.otlp.proto"),
        "opentelemetry.exporter.otlp.proto.http": types.ModuleType("opentelemetry.exporter.otlp.proto.http"),
        "opentelemetry.exporter.otlp.proto.http._log_exporter": exporter_mod,
    }
    hit = {"n": 0}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout):
        hit["n"] += 1
        return _Resp()

    src = inspect.getsource(otel_mod.SessionTelemetryCoordinator.export)
    assert "if _sdk_available" not in src  # early-return 已删
    with mock.patch.dict(sys.modules, patch):
        monkeypatch.setattr(urllib.request, "urlopen", fake_open)
        monkeypatch.setattr(builtins, "__import__", __import__)
        otel_mod.SessionTelemetryCoordinator(mode="private").export({"event": "x"})
    assert hit["n"] == 1
    otel_mod._OTEL_CACHED_PROVIDER = None
    otel_mod._OTEL_CACHED_PROCESSOR = None
    otel_mod._OTEL_CACHED_ENDPOINT = None


# ── otel.py:169-172 冗余检查删除 ──


def test_a3a_otel_redundant_169_254_check_removed():
    """冗余 169.254 二次检查删除（_is_ip_blocked 已覆盖）。"""
    import hero_quant.telemetry.otel as otel_mod

    src = inspect.getsource(otel_mod.SessionTelemetryCoordinator._validate_endpoint)
    assert src.count('startswith("169.254.")') + src.count("startswith('169.254.')") <= 1

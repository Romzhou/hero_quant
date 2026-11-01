"""PR-151 Redis 口令注入：逻辑名 `redis_password` → DSN，vault/kms 不回落 env。

覆盖：有 userinfo 无口令注入 / 已带口令原样 / 无 userinfo 原样 / 非 redis scheme 原样 /
provider 取不到口令原样 / vault+kms 不注入 / 特殊字符百分号编码 / 日志不含真值与完整 DSN。

假值一律 `replace-with-*` 形态（`gitleaks.toml` 窄口径放行），绝不读取 .env 真值。
"""

from __future__ import annotations

import logging
from urllib.parse import unquote, urlsplit

import pytest

from hero_quant.secrets.redis import apply_redis_password

_CLEAN_ENVS = (
    "HERO_REDIS_PASSWORD",
    "HERO_SECRETS_BACKEND",
    "HERO_REDIS_DSN",
    "HERO_REDIS_HOST",
    "HERO_REDIS_PORT",
    "HERO_REDIS_DB",
    "REDIS_URL",
)

# 含 DSN 保留字符 @ : / %，验证「编码后写入」而不是原样拼接
_SECRET = "replace-with-redis-p@ss:w/rd%"
_DSN = "redis://hero_user@redis.internal:6379/0"
_LOGGER = "hero_quant.secrets.redis"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _CLEAN_ENVS:
        monkeypatch.delenv(name, raising=False)
    yield


def test_password_injected_when_dsn_has_user_and_no_password(monkeypatch):
    monkeypatch.setenv("HERO_REDIS_PASSWORD", _SECRET)
    dsn = apply_redis_password(_DSN)
    assert dsn is not None
    parts = urlsplit(dsn)
    assert parts.scheme == "redis"
    assert parts.username == "hero_user"
    assert unquote(parts.password or "") == _SECRET, "口令必须能按 RFC 3986 解回原值"
    assert _SECRET not in dsn, "特殊字符必须百分号编码，不能原样出现在 DSN 里"
    assert parts.hostname == "redis.internal"
    assert parts.port == 6379
    assert parts.path == "/0"


def test_rediss_scheme_also_injected(monkeypatch):
    monkeypatch.setenv("HERO_REDIS_PASSWORD", _SECRET)
    dsn = apply_redis_password("rediss://hero_user@redis.internal:6380/1")
    parts = urlsplit(dsn or "")
    assert parts.scheme == "rediss"
    assert unquote(parts.password or "") == _SECRET


def test_explicit_password_wins(monkeypatch):
    monkeypatch.setenv("HERO_REDIS_PASSWORD", _SECRET)
    explicit = "redis://hero_user:explicit-pass@redis.internal:6379/0"
    assert apply_redis_password(explicit) == explicit


def test_no_userinfo_is_left_alone(monkeypatch):
    monkeypatch.setenv("HERO_REDIS_PASSWORD", _SECRET)
    dsn = "redis://redis.internal:6379/0"
    assert apply_redis_password(dsn) == dsn


def test_password_only_userinfo_gets_password(monkeypatch):
    # redis 允许「只有口令」的空用户名形态：netloc 有 @ 即算有 userinfo
    monkeypatch.setenv("HERO_REDIS_PASSWORD", _SECRET)
    dsn = apply_redis_password("redis://@redis.internal:6379/0")
    parts = urlsplit(dsn or "")
    assert (parts.username or "") == ""
    assert unquote(parts.password or "") == _SECRET


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://hero_user@db.internal:5432/heroquant",
        "http://hero_user@cache.internal:6379/0",
        "unix:///tmp/redis.sock",
    ],
)
def test_non_redis_scheme_is_left_alone(monkeypatch, dsn):
    monkeypatch.setenv("HERO_REDIS_PASSWORD", _SECRET)
    assert apply_redis_password(dsn) == dsn


def test_missing_provider_password_keeps_dsn_unchanged():
    assert apply_redis_password(_DSN) == _DSN


@pytest.mark.parametrize("backend", ["vault", "kms"])
def test_reserved_backend_does_not_inject(monkeypatch, backend, caplog):
    # 关键反证：env 里有明文口令，但后端是 vault/kms → 绝不回落到 env
    monkeypatch.setenv("HERO_SECRETS_BACKEND", backend)
    monkeypatch.setenv("HERO_REDIS_PASSWORD", _SECRET)
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    assert apply_redis_password(_DSN) == _DSN
    assert "secrets_backend_unavailable" in caplog.text


def test_query_params_preserved(monkeypatch):
    monkeypatch.setenv("HERO_REDIS_PASSWORD", "simple-pass")
    dsn = apply_redis_password("redis://hero_user@redis.internal:6379/2?ssl_cert_reqs=required")
    assert dsn is not None
    assert dsn.endswith("?ssl_cert_reqs=required")
    assert ":simple-pass@" in dsn


def test_invalid_port_keeps_dsn_and_warns(monkeypatch, caplog):
    monkeypatch.setenv("HERO_REDIS_PASSWORD", _SECRET)
    dsn = "redis://hero_user@redis.internal:not-a-port/0"
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    assert apply_redis_password(dsn) == dsn
    assert "invalid_port" in caplog.text


def test_logs_carry_host_only_never_secret(monkeypatch, caplog):
    monkeypatch.setenv("HERO_REDIS_PASSWORD", _SECRET)
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    assert apply_redis_password(_DSN) is not None
    text = caplog.text
    assert "secrets.redis_password_injected" in text and "redis.internal" in text, "日志必须能定位到 host"
    assert _SECRET not in text, "口令真值绝不允许进日志"
    assert unquote(_SECRET) not in text
    assert _DSN not in text, "完整 DSN（含用户名）也不入日志"


@pytest.mark.parametrize("dsn", [None, ""])
def test_empty_input_returns_as_is(dsn):
    assert apply_redis_password(dsn) == dsn

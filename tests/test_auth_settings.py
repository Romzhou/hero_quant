"""W1-02：auth 环境变量收口到 config.settings（唯一 env gate）。

验证 Settings 字段解析（含非法值回退）、OIDCSettings/auth_enabled 无参路径走 Settings。
"""

from __future__ import annotations

import pytest

from hero_quant.auth.errors import AuthConfigError
from hero_quant.auth.oidc import AuthMode, OIDCSettings, auth_enabled
from hero_quant.auth.quota import default_quota, reset_default_quota

ISSUER = "https://idp.example/realms/hero"
AUDIENCE = "hero-api"


def _fresh_settings():
    from hero_quant.config.settings import get_settings

    get_settings.cache_clear()
    return get_settings()


def test_settings_parses_auth_env(monkeypatch):
    monkeypatch.setenv("HERO_AUTH_MODE", "off")
    monkeypatch.setenv("HERO_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("HERO_OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("HERO_OIDC_ALGORITHMS", "rs256, hs256")
    monkeypatch.setenv("HERO_OIDC_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("HERO_OIDC_JWKS_TTL_SECONDS", "120")
    monkeypatch.setenv("HERO_TENANT_REQUESTS_PER_MINUTE", "9")
    monkeypatch.setenv("HERO_TENANT_DAILY_TOKEN_BUDGET", "1234")

    settings = _fresh_settings()
    assert settings.auth_mode == "off"
    assert settings.oidc_issuer == ISSUER
    assert settings.oidc_audience == AUDIENCE
    assert settings.oidc_jwks_url == f"{ISSUER}/protocol/openid-connect/certs"
    assert settings.oidc_algorithms == ("RS256", "HS256")
    assert settings.oidc_leeway_seconds == 30
    assert settings.oidc_jwks_ttl_seconds == 120
    assert settings.tenant_requests_per_minute == 9
    assert settings.tenant_daily_token_budget == 1234


def test_settings_invalid_auth_mode_falls_back_to_enforce(monkeypatch):
    monkeypatch.setenv("HERO_AUTH_MODE", "banana")
    assert _fresh_settings().auth_mode == "enforce"


def test_settings_out_of_range_int_falls_back(monkeypatch):
    monkeypatch.setenv("HERO_OIDC_LEEWAY_SECONDS", "9999")
    monkeypatch.setenv("HERO_TENANT_REQUESTS_PER_MINUTE", "0")
    settings = _fresh_settings()
    assert settings.oidc_leeway_seconds == 60
    assert settings.tenant_requests_per_minute == 60


def test_oidc_settings_from_env_uses_settings_gate(monkeypatch):
    monkeypatch.setenv("HERO_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("HERO_OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("HERO_OIDC_ALGORITHMS", "RS256")
    monkeypatch.setenv("HERO_AUTH_MODE", "off")
    _fresh_settings()

    settings = OIDCSettings.from_env()
    assert settings.issuer == ISSUER
    assert settings.audience == AUDIENCE
    assert settings.mode is AuthMode.OFF
    assert settings.jwks_url == f"{ISSUER}/protocol/openid-connect/certs"


def test_oidc_settings_from_env_without_issuer_fails_closed(monkeypatch):
    monkeypatch.delenv("HERO_OIDC_ISSUER", raising=False)
    monkeypatch.setenv("HERO_OIDC_AUDIENCE", AUDIENCE)
    _fresh_settings()
    with pytest.raises(AuthConfigError):
        OIDCSettings.from_env()


def test_auth_enabled_uses_settings_gate(monkeypatch):
    monkeypatch.setenv("HERO_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("HERO_OIDC_AUDIENCE", AUDIENCE)
    _fresh_settings()
    assert auth_enabled() is True

    monkeypatch.setenv("HERO_AUTH_MODE", "off")
    _fresh_settings()
    assert auth_enabled() is False


def test_default_quota_uses_settings_gate(monkeypatch):
    monkeypatch.setenv("HERO_TENANT_REQUESTS_PER_MINUTE", "11")
    monkeypatch.setenv("HERO_TENANT_DAILY_TOKEN_BUDGET", "777")
    _fresh_settings()
    reset_default_quota()
    try:
        policy = default_quota().policy
        assert policy.requests_per_minute == 11
        assert policy.daily_token_budget == 777
    finally:
        reset_default_quota()

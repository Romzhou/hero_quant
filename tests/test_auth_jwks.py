"""W1/PR-101：JWKS 解析/缓存与 OIDC 组装的真实链路（本地 RSA 密钥，不出网）。

覆盖：JWK(n/e)->公钥、未知 kid 强制刷新与冷却、拉取失败保留旧键、超陈旧清空、
无 kid 多键歧义拒绝、OIDCSettings.from_env 默认值与 fail-closed。
"""

from __future__ import annotations

import base64
import json

import pytest

from hero_quant.auth.errors import AuthConfigError
from hero_quant.auth.jwks import JWKSCache, parse_jwks
from hero_quant.auth.jwt import JWTConfig, JWTVerifier
from hero_quant.auth.oidc import AuthMode, OIDCSettings, auth_enabled, build_verifier, get_auth_mode

ISSUER = "https://idp.example/realms/hero"
AUDIENCE = "hero-api"
TOKEN_NOW = 1_000_000.0


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _public_jwk(public_key, kid: str = "k1") -> dict:
    numbers = public_key.public_numbers()
    return {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _Fetcher:
    def __init__(self, document: dict) -> None:
        self.document = document
        self.calls = 0
        self.fail = False

    def __call__(self, url: str) -> dict:
        self.calls += 1
        if self.fail:
            raise ValueError("idp unreachable")
        return self.document


def _sign_rs256(private_key, kid: str = "k1", sub: str = "user-1") -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT", "kid": kid}, separators=(",", ":")).encode())
    claims = {
        "sub": sub,
        "tenant_id": "tenant-a",
        "roles": ["analyst"],
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": int(TOKEN_NOW + 300),
    }
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signature = private_key.sign(f"{header}.{payload}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64url(signature)}"


@pytest.fixture()
def rsa_keys():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def test_parse_jwks_skips_unusable_entries(rsa_keys):
    _private, public_key = rsa_keys
    document = {
        "keys": [
            {**_public_jwk(public_key, kid="enc-key"), "use": "enc"},
            _public_jwk(public_key, kid="ok-key"),
            {"kty": "EC", "kid": "ec-key", "use": "sig"},
            {"kty": "RSA", "kid": "broken", "use": "sig"},
        ]
    }
    keys = parse_jwks(document)
    assert set(keys) == {"ok-key"}


def test_parse_jwks_rejects_empty_documents():
    with pytest.raises(AuthConfigError):
        parse_jwks({"keys": []})
    with pytest.raises(AuthConfigError):
        parse_jwks({})


def test_cache_verifies_token_end_to_end(rsa_keys):
    private_key, public_key = rsa_keys
    fetcher = _Fetcher({"keys": [_public_jwk(public_key)]})
    cache = JWKSCache("https://idp.example/certs", fetcher=fetcher, clock=_Clock())
    config = JWTConfig(issuer=ISSUER, audience=AUDIENCE, algorithms=("RS256",), leeway_seconds=0)
    verifier = JWTVerifier(cache.get_key, config, clock=lambda: TOKEN_NOW)

    claims = verifier.verify(_sign_rs256(private_key))
    assert claims["sub"] == "user-1"
    assert fetcher.calls == 1


def test_unknown_kid_forces_one_refresh_then_cools_down(rsa_keys):
    private_key, public_key = rsa_keys
    fetcher = _Fetcher({"keys": [_public_jwk(public_key, "k1")]})
    cache = JWKSCache("https://idp.example/certs", fetcher=fetcher, clock=_Clock())

    assert cache.get_key("k1") is not None
    assert fetcher.calls == 1

    # IdP 轮换：新 kid 立即可见（强制刷新一次）
    fetcher.document = {"keys": [_public_jwk(public_key, "k1"), _public_jwk(public_key, "k2")]}
    assert cache.get_key("k2") is not None
    assert fetcher.calls == 2

    # 冷却期内伪造 kid 不得再触发拉取
    assert cache.get_key("k3") is None
    assert fetcher.calls == 2


def test_refresh_failure_keeps_stale_keys(rsa_keys):
    _private, public_key = rsa_keys
    clock = _Clock()
    fetcher = _Fetcher({"keys": [_public_jwk(public_key)]})
    cache = JWKSCache("https://idp.example/certs", fetcher=fetcher, ttl_seconds=10, min_refresh_seconds=1, clock=clock)

    assert cache.get_key("k1") is not None
    clock.now += 15
    fetcher.fail = True
    assert cache.get_key("k1") is not None
    assert fetcher.calls == 2


def test_keys_dropped_after_max_stale(rsa_keys):
    _private, public_key = rsa_keys
    clock = _Clock()
    fetcher = _Fetcher({"keys": [_public_jwk(public_key)]})
    cache = JWKSCache(
        "https://idp.example/certs",
        fetcher=fetcher,
        ttl_seconds=10,
        max_stale_seconds=20,
        min_refresh_seconds=1,
        clock=clock,
    )

    assert cache.get_key("k1") is not None
    clock.now += 31
    fetcher.fail = True
    assert cache.get_key("k1") is None


def test_multiple_keys_without_kid_is_ambiguous(rsa_keys):
    _private, public_key = rsa_keys
    fetcher = _Fetcher({"keys": [_public_jwk(public_key, "k1"), _public_jwk(public_key, "k2")]})
    cache = JWKSCache("https://idp.example/certs", fetcher=fetcher, clock=_Clock())
    assert cache.get_key(None) is None


def test_oidc_build_verifier_from_settings(rsa_keys):
    private_key, public_key = rsa_keys
    fetcher = _Fetcher({"keys": [_public_jwk(public_key)]})
    settings = OIDCSettings(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url="https://idp.example/certs",
        algorithms=("RS256",),
        leeway_seconds=0,
    )
    verifier = build_verifier(settings, fetcher=fetcher, token_clock=lambda: TOKEN_NOW, cache_clock=_Clock())
    assert verifier.verify(_sign_rs256(private_key))["tenant_id"] == "tenant-a"


def test_oidc_settings_from_env_defaults_keycloak_certs_url():
    settings = OIDCSettings.from_env({"HERO_OIDC_ISSUER": ISSUER, "HERO_OIDC_AUDIENCE": AUDIENCE})
    assert settings.jwks_url == f"{ISSUER}/protocol/openid-connect/certs"
    assert settings.algorithms == ("RS256",)
    assert settings.mode is AuthMode.ENFORCE


def test_oidc_settings_require_audience():
    with pytest.raises(AuthConfigError):
        OIDCSettings.from_env({"HERO_OIDC_ISSUER": ISSUER})


def test_auth_mode_invalid_value_falls_back_to_enforce():
    assert get_auth_mode({}) is AuthMode.ENFORCE
    assert get_auth_mode({"HERO_AUTH_MODE": "off"}) is AuthMode.OFF
    assert get_auth_mode({"HERO_AUTH_MODE": "banana"}) is AuthMode.ENFORCE


def test_auth_enabled_requires_mode_and_oidc_config():
    assert not auth_enabled({})
    assert not auth_enabled({"HERO_AUTH_MODE": "off", "HERO_OIDC_ISSUER": ISSUER, "HERO_OIDC_AUDIENCE": AUDIENCE})
    assert auth_enabled({"HERO_OIDC_ISSUER": ISSUER, "HERO_OIDC_AUDIENCE": AUDIENCE})


def test_jwks_rsa_without_cryptography_fails_closed(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "cryptography" or name.startswith("cryptography."):
            raise ImportError("cryptography blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    document = {"keys": [{"kty": "RSA", "kid": "k1", "use": "sig", "n": "AQAB", "e": "AQAB"}]}
    with pytest.raises(AuthConfigError) as exc:
        parse_jwks(document)
    assert "cryptography_not_installed" in str(exc.value)

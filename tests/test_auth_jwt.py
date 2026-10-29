"""W1/PR-101：JWT 验签（算法白名单、签名、exp/nbf/iat、iss/aud、RS256）。

只使用本地密钥与固定时钟，不出网；RS256 在缺少 cryptography 时 skip。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from hero_quant.auth.errors import AuthConfigError, InvalidToken
from hero_quant.auth.jwt import JWTConfig, JWTVerifier

NOW = 1_000_000.0
SECRET = "unit-test-secret-value-32-bytes-min"
ISSUER = "https://idp.example/realms/hero"
AUDIENCE = "hero-api"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _base_claims(**overrides):
    claims = {
        "sub": "user-1",
        "tenant_id": "tenant-a",
        "roles": ["analyst"],
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": int(NOW + 300),
    }
    claims.update(overrides)
    return claims


def _sign_hs256(claims, *, key: str = SECRET, kid: str | None = "k1", alg: str = "HS256") -> str:
    header = {"alg": alg, "typ": "JWT"}
    if kid is not None:
        header["kid"] = kid
    header_segment = _b64url(json.dumps(header, separators=(",", ":")).encode())
    payload_segment = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature = hmac.new(key.encode(), signing_input, hashlib.sha256).digest()
    return f"{header_segment}.{payload_segment}.{_b64url(signature)}"


def _verifier(*, algorithms=("HS256",), leeway: int = 0, require_exp: bool = True) -> JWTVerifier:
    config = JWTConfig(
        issuer=ISSUER,
        audience=AUDIENCE,
        algorithms=algorithms,
        leeway_seconds=leeway,
        require_exp=require_exp,
    )
    return JWTVerifier(lambda kid: SECRET if kid in ("k1", None) else None, config, clock=lambda: NOW)


def test_valid_token_returns_claims():
    claims = _verifier().verify(_sign_hs256(_base_claims()))
    assert claims["sub"] == "user-1"
    assert claims["tenant_id"] == "tenant-a"


def test_tampered_payload_rejected():
    token = _sign_hs256(_base_claims())
    header, _payload, signature = token.split(".")
    forged = _b64url(json.dumps(_base_claims(sub="attacker"), separators=(",", ":")).encode())
    with pytest.raises(InvalidToken) as exc:
        _verifier().verify(f"{header}.{forged}.{signature}")
    assert str(exc.value) == "bad_signature"


def test_expired_token_rejected():
    with pytest.raises(InvalidToken) as exc:
        _verifier().verify(_sign_hs256(_base_claims(exp=int(NOW - 1))))
    assert str(exc.value) == "token_expired"


def test_leeway_allows_recently_expired():
    claims = _sign_hs256(_base_claims(exp=int(NOW - 30)))
    assert _verifier(leeway=60).verify(claims)["sub"] == "user-1"


def test_missing_exp_rejected():
    claims = _base_claims()
    claims.pop("exp")
    with pytest.raises(InvalidToken) as exc:
        _verifier().verify(_sign_hs256(claims))
    assert str(exc.value) == "missing_exp"


def test_missing_exp_allowed_when_not_required():
    claims = _base_claims()
    claims.pop("exp")
    assert _verifier(require_exp=False).verify(_sign_hs256(claims))["sub"] == "user-1"


def test_bool_exp_rejected():
    with pytest.raises(InvalidToken) as exc:
        _verifier().verify(_sign_hs256(_base_claims(exp=True)))
    assert str(exc.value) == "malformed_exp"


def test_nbf_in_future_rejected():
    with pytest.raises(InvalidToken) as exc:
        _verifier().verify(_sign_hs256(_base_claims(nbf=int(NOW + 120))))
    assert str(exc.value) == "token_not_yet_valid"


def test_iat_in_future_rejected():
    with pytest.raises(InvalidToken) as exc:
        _verifier().verify(_sign_hs256(_base_claims(iat=int(NOW + 120))))
    assert str(exc.value) == "token_issued_in_future"


def test_wrong_issuer_rejected():
    with pytest.raises(InvalidToken) as exc:
        _verifier().verify(_sign_hs256(_base_claims(iss="https://evil.example/realms/hero")))
    assert str(exc.value) == "issuer_mismatch"


def test_audience_list_matches():
    token = _sign_hs256(_base_claims(aud=["other-api", AUDIENCE]))
    assert _verifier().verify(token)["sub"] == "user-1"


def test_audience_mismatch_rejected():
    with pytest.raises(InvalidToken) as exc:
        _verifier().verify(_sign_hs256(_base_claims(aud="other-api")))
    assert str(exc.value) == "audience_mismatch"


def test_alg_none_rejected():
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT", "kid": "k1"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps(_base_claims(), separators=(",", ":")).encode())
    with pytest.raises(InvalidToken) as exc:
        _verifier().verify(f"{header}.{payload}.{_b64url(b'x')}")
    assert str(exc.value) == "algorithm_not_allowed"


def test_alg_not_configured_rejected():
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT", "kid": "k1"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps(_base_claims(), separators=(",", ":")).encode())
    with pytest.raises(InvalidToken) as exc:
        _verifier().verify(f"{header}.{payload}.{_b64url(b'x')}")
    assert str(exc.value) == "algorithm_not_allowed"


def test_unknown_kid_rejected():
    with pytest.raises(InvalidToken) as exc:
        _verifier().verify(_sign_hs256(_base_claims(), kid="rotated-key"))
    assert str(exc.value) == "unknown_signing_key"


def test_malformed_tokens_rejected():
    verifier = _verifier()
    for token in ("", "a.b", "a.b.c.d", "not-a-jwt", "a.b.c"):
        with pytest.raises(InvalidToken):
            verifier.verify(token)


def test_unsupported_algorithm_config_rejected():
    with pytest.raises(AuthConfigError):
        JWTConfig(algorithms=("ES256",))
    with pytest.raises(AuthConfigError):
        JWTConfig(algorithms=())


def test_rs256_roundtrip_and_tamper():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT", "kid": "k1"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps(_base_claims(), separators=(",", ":")).encode())
    signature = private_key.sign(f"{header}.{payload}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    token = f"{header}.{payload}.{_b64url(signature)}"

    config = JWTConfig(issuer=ISSUER, audience=AUDIENCE, algorithms=("RS256",), leeway_seconds=0)
    verifier = JWTVerifier(lambda kid: public_key if kid in ("k1", None) else None, config, clock=lambda: NOW)
    assert verifier.verify(token)["sub"] == "user-1"

    forged_payload = _b64url(json.dumps(_base_claims(sub="attacker"), separators=(",", ":")).encode())
    with pytest.raises(InvalidToken) as exc:
        verifier.verify(f"{header}.{forged_payload}.{_b64url(signature)}")
    assert str(exc.value) == "bad_signature"

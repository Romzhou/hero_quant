"""auth.jwt — 最小 JWT 验签器（HS256/384/512 与 RS256/384/512），零新依赖。

职责：解析并校验 Bearer JWT 的结构、算法、签名与标准声明（exp/nbf/iat/iss/aud）。
架构位置：auth 包；由 oidc.build_verifier 组装 JWKS 解析器后交给 deps/middleware。
关键设计：
- 算法白名单先行：拒绝 none 与未配置算法，杜绝 alg 混淆；
- 签名校验使用常量时间比较（HMAC）或 cryptography 公钥验签（RSA）；
- 所有失败收敛为 InvalidToken（客户端错误）；密钥/依赖缺失收敛为 AuthConfigError（服务端错误）；
- 数值声明拒绝 bool/字符串/NaN，避免解析歧义绕过有效期。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from hero_quant.auth.errors import AuthConfigError, InvalidToken

_HMAC_ALGORITHMS: dict[str, Any] = {
    "HS256": hashlib.sha256,
    "HS384": hashlib.sha384,
    "HS512": hashlib.sha512,
}
_RSA_ALGORITHMS: dict[str, str] = {
    "RS256": "SHA256",
    "RS384": "SHA384",
    "RS512": "SHA512",
}
SUPPORTED_ALGORITHMS: frozenset[str] = frozenset(_HMAC_ALGORITHMS) | frozenset(_RSA_ALGORITHMS)

# 中文：宽松但有界——只接受 base64url 字母表，且 token 长度上限防解析型 DoS。
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_TOKEN_LENGTH = 32_768

# 中文：kid -> 验签材料（RSA 公钥对象 / HMAC 密钥 bytes）。
KeyResolver = Callable[[str | None], Any]


@dataclass(frozen=True)
class JWTConfig:
    """验签策略；构造即校验，非法算法/负 leeway 直接失败（不静默降级）。"""

    issuer: str | None = None
    audience: str | tuple[str, ...] | None = None
    algorithms: tuple[str, ...] = ("RS256",)
    leeway_seconds: int = 60
    require_exp: bool = True

    def __post_init__(self) -> None:
        algorithms = tuple(str(alg).strip().upper() for alg in self.algorithms)
        if not algorithms:
            raise AuthConfigError("no_jwt_algorithms_configured")
        unsupported = [alg for alg in algorithms if alg not in SUPPORTED_ALGORITHMS]
        if unsupported:
            raise AuthConfigError(f"unsupported_jwt_algorithm:{','.join(unsupported)}")
        try:
            leeway = int(self.leeway_seconds)
        except (TypeError, ValueError) as e:
            raise AuthConfigError("invalid_jwt_leeway") from e
        if leeway < 0:
            raise AuthConfigError("negative_jwt_leeway")
        object.__setattr__(self, "algorithms", algorithms)
        object.__setattr__(self, "leeway_seconds", leeway)


def _b64url_decode(segment: str) -> bytes:
    """严格 base64url 解码：空段/非字母表/长度非法一律 InvalidToken。"""
    if not isinstance(segment, str) or not segment or not _B64URL_RE.match(segment):
        raise InvalidToken("malformed_token")
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except (binascii.Error, ValueError) as e:
        raise InvalidToken("malformed_token") from e


def _decode_json_segment(segment: str, name: str) -> dict[str, Any]:
    """解码 JWT 的 header/payload 段为 JSON 对象。"""
    raw = _b64url_decode(segment)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise InvalidToken(f"malformed_{name}") from e
    if not isinstance(data, dict):
        raise InvalidToken(f"malformed_{name}")
    return data


def _numeric_date(value: Any, *, name: str, required: bool) -> float | None:
    """解析 NumericDate：仅接受 int/float（排除 bool），拒绝 NaN/Inf 与字符串。"""
    if value is None:
        if required:
            raise InvalidToken(f"missing_{name}")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidToken(f"malformed_{name}")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):  # NaN / Inf
        raise InvalidToken(f"malformed_{name}")
    return number


def _coerce_rsa_public_key(key: Any) -> Any:
    """把 PEM bytes 或公钥对象归一为可验签对象；类型不符按服务端配置错误处理。"""
    if hasattr(key, "verify"):
        return key
    if isinstance(key, (bytes, bytearray, str)):
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_public_key
        except ImportError as e:  # pragma: no cover — 生产缺 cryptography 时的明确报错
            raise AuthConfigError("cryptography_not_installed") from e
        try:
            return load_pem_public_key(bytes(key) if isinstance(key, (bytes, bytearray)) else key.encode())
        except (ValueError, TypeError) as e:
            raise AuthConfigError("invalid_rsa_public_key") from e
    raise AuthConfigError("invalid_rsa_public_key")


def _verify_rsa(algorithm: str, key: Any, signing_input: bytes, signature: bytes) -> None:
    """RSA PKCS#1 v1.5 验签；签名不符转 InvalidToken，依赖缺失转 AuthConfigError。"""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as e:  # pragma: no cover — cryptography 属 dev/lock 依赖，缺失需显式暴露
        raise AuthConfigError("cryptography_not_installed") from e
    public_key = _coerce_rsa_public_key(key)
    digest = getattr(hashes, _RSA_ALGORITHMS[algorithm])()
    try:
        public_key.verify(signature, signing_input, padding.PKCS1v15(), digest)
    except InvalidSignature:
        raise InvalidToken("bad_signature") from None


def _verify_hmac(algorithm: str, key: Any, signing_input: bytes, signature: bytes) -> None:
    """HMAC 验签；密钥必须为 bytes/str，常量时间比较。"""
    if isinstance(key, str):
        secret = key.encode("utf-8")
    elif isinstance(key, (bytes, bytearray)):
        secret = bytes(key)
    else:
        raise AuthConfigError("invalid_hmac_key")
    expected = hmac.new(secret, signing_input, _HMAC_ALGORITHMS[algorithm]).digest()
    if not hmac.compare_digest(expected, signature):
        raise InvalidToken("bad_signature")


class JWTVerifier:
    """无状态验签器：resolve_key(kid) 提供验签材料，clock 可注入以便测试。"""

    def __init__(
        self,
        resolve_key: KeyResolver,
        config: JWTConfig,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not callable(resolve_key):
            raise TypeError("resolve_key must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._resolve_key = resolve_key
        self._config = config
        self._clock = clock

    @property
    def config(self) -> JWTConfig:
        return self._config

    def verify(self, token: str) -> Mapping[str, Any]:
        """校验 token 并返回 claims；任何不合规都抛 InvalidToken（不返回半可信结果）。"""
        if not isinstance(token, str) or not token.strip():
            raise InvalidToken("missing_token")
        token = token.strip()
        if len(token) > MAX_TOKEN_LENGTH:
            raise InvalidToken("token_too_large")
        parts = token.split(".")
        if len(parts) != 3:
            raise InvalidToken("malformed_token")
        header_segment, payload_segment, signature_segment = parts

        # 中文：先解析 header/payload（两者均无可信度），再校验算法与签名。
        header = _decode_json_segment(header_segment, "header")
        claims = _decode_json_segment(payload_segment, "payload")

        algorithm = header.get("alg")
        if not isinstance(algorithm, str) or not algorithm.strip():
            raise InvalidToken("missing_alg")
        algorithm = algorithm.strip().upper()
        if algorithm == "NONE" or algorithm not in self._config.algorithms:
            raise InvalidToken("algorithm_not_allowed")

        kid = header.get("kid")
        if kid is not None and not isinstance(kid, str):
            raise InvalidToken("malformed_kid")
        key = self._resolve_key(kid.strip() if isinstance(kid, str) else None)
        if key is None:
            raise InvalidToken("unknown_signing_key")

        signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
        signature = _b64url_decode(signature_segment)
        if algorithm in _HMAC_ALGORITHMS:
            _verify_hmac(algorithm, key, signing_input, signature)
        else:
            _verify_rsa(algorithm, key, signing_input, signature)

        self._validate_claims(claims)
        return claims

    def _validate_claims(self, claims: Mapping[str, Any]) -> None:
        """标准声明校验：exp 必须存在（可配置）、nbf/iat 容差、iss 精确、aud 交集。"""
        now = float(self._clock())
        leeway = float(self._config.leeway_seconds)

        expires_at = _numeric_date(claims.get("exp"), name="exp", required=self._config.require_exp)
        if expires_at is not None and now >= expires_at + leeway:
            raise InvalidToken("token_expired")

        not_before = _numeric_date(claims.get("nbf"), name="nbf", required=False)
        if not_before is not None and now < not_before - leeway:
            raise InvalidToken("token_not_yet_valid")

        issued_at = _numeric_date(claims.get("iat"), name="iat", required=False)
        if issued_at is not None and issued_at > now + leeway:
            raise InvalidToken("token_issued_in_future")

        if self._config.issuer:
            issuer = claims.get("iss")
            if not isinstance(issuer, str) or issuer != self._config.issuer:
                raise InvalidToken("issuer_mismatch")

        if self._config.audience:
            audience = self._config.audience
            expected = {audience} if isinstance(audience, str) else set(audience)
            raw_aud = claims.get("aud")
            if isinstance(raw_aud, str):
                actual = {raw_aud}
            elif isinstance(raw_aud, list):
                actual = {item for item in raw_aud if isinstance(item, str)}
            else:
                actual = set()
            if not actual & expected:
                raise InvalidToken("audience_mismatch")


__all__ = ["JWTConfig", "JWTVerifier", "SUPPORTED_ALGORITHMS"]

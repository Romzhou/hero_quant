"""auth.jwks — JWKS 公钥缓存（TTL + 单飞刷新 + 未知 kid 主动刷新 + 有限陈旧容忍）。

职责：按 kid 解析 IdP 公钥，供 JWTVerifier 校验 RS* 签名。
架构位置：auth 包；由 oidc.build_verifier 注入 JWTVerifier。
关键设计：
- 刷新受线程锁保护，并设最小刷新间隔（默认 30s），防未知 kid 打爆 IdP；
- 刷新失败保留旧键（IdP 抖动不应让全员掉线），超过 max_stale 后清空（fail-closed）；
- 无 kid 时仅当缓存恰好一把键才放行，多键歧义一律拒绝；
- JWKS 文档结构非法/无可用键按 AuthConfigError 记录，不静默返回空结果。
"""

from __future__ import annotations

import base64
import binascii
import logging
import threading
import time
from collections.abc import Mapping
from typing import Any, Callable
from urllib.parse import urlsplit

from hero_quant.auth.errors import AuthConfigError

logger = logging.getLogger(__name__)

DEFAULT_JWKS_TTL_SECONDS = 300
DEFAULT_MIN_REFRESH_SECONDS = 30
DEFAULT_MAX_STALE_SECONDS = 3600

JwksFetcher = Callable[[str], Mapping[str, Any]]


def _b64url_bytes(value: Any) -> bytes:
    """JWK 字段解码（严格）：非字符串/空/非法字母表一律 ValueError。"""
    if not isinstance(value, str) or not value:
        raise ValueError("empty base64url value")
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError("invalid base64url value") from e


def _b64url_uint(value: Any) -> int:
    """JWK 的 n/e 等无符号整数（大端）。"""
    raw = _b64url_bytes(value)
    if not raw:
        raise ValueError("empty integer")
    return int.from_bytes(raw, "big")


def _public_key_from_jwk(jwk: Mapping[str, Any]) -> Any:
    """JWK -> 验签材料；RSA 用 cryptography 构造，oct 直接返回对称密钥。"""
    key_type = jwk.get("kty")
    if key_type == "RSA":
        try:
            from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
        except ImportError as e:  # pragma: no cover — 生产缺 cryptography 时显式暴露
            raise AuthConfigError("cryptography_not_installed") from e
        modulus = _b64url_uint(jwk.get("n"))
        exponent = _b64url_uint(jwk.get("e"))
        try:
            return RSAPublicNumbers(exponent, modulus).public_key()
        except (ValueError, TypeError) as e:
            raise AuthConfigError("invalid_rsa_jwk") from e
    if key_type == "oct":
        return _b64url_bytes(jwk.get("k"))
    raise AuthConfigError(f"unsupported_jwk_kty:{key_type}")


def parse_jwks(document: Mapping[str, Any]) -> dict[str, Any]:
    """解析 JWKS 文档为 {kid: key}；跳过非签名用途/无 kid/损坏条目，全空则视为配置错误。"""
    if not isinstance(document, Mapping):
        raise AuthConfigError("jwks_not_object")
    raw_keys = document.get("keys")
    if not isinstance(raw_keys, list):
        raise AuthConfigError("jwks_missing_keys")
    keys: dict[str, Any] = {}
    for jwk in raw_keys:
        if not isinstance(jwk, Mapping):
            continue
        if jwk.get("use") not in (None, "sig"):
            continue
        key_ops = jwk.get("key_ops")
        if isinstance(key_ops, list) and "verify" not in key_ops:
            continue
        kid = jwk.get("kid")
        if not isinstance(kid, str) or not kid.strip():
            logger.warning("auth.jwks_key_without_kid_skipped")
            continue
        try:
            public_key = _public_key_from_jwk(jwk)
        except AuthConfigError as e:
            # 中文：缺 cryptography 是服务端依赖缺失（503），不能降级成「跳过该键」后
            # 以空 JWKS 收敛成 401，让客户端误以为 token 无效。
            if "cryptography_not_installed" in str(e):
                raise
            logger.warning("auth.jwks_key_unusable kid=%s error=%s", kid, e)
            continue
        except (ValueError, TypeError) as e:
            logger.warning("auth.jwks_key_unusable kid=%s error=%s", kid, e)
            continue
        if kid in keys:
            logger.warning("auth.jwks_duplicate_kid kid=%s", kid)
        keys[kid] = public_key
    if not keys:
        raise AuthConfigError("jwks_no_usable_keys")
    return keys


def _http_fetch(url: str) -> Mapping[str, Any]:
    """默认拉取器：短超时、不跟随重定向、仅接受 200 JSON。"""
    try:
        import httpx
    except ImportError as e:  # pragma: no cover — httpx 是核心依赖
        raise AuthConfigError("httpx_not_installed") from e
    try:
        with httpx.Client(timeout=5.0, follow_redirects=False) as client:
            response = client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as e:
        raise AuthConfigError(f"jwks_fetch_failed:{type(e).__name__}") from e
    if response.status_code != 200:
        raise AuthConfigError(f"jwks_http_status:{response.status_code}")
    try:
        payload = response.json()
    except ValueError as e:
        raise AuthConfigError("jwks_invalid_json") from e
    return payload


class JWKSCache:
    """按 kid 提供公钥的线程安全缓存；clock 可注入便于测试过期语义。"""

    def __init__(
        self,
        url: str,
        *,
        ttl_seconds: int = DEFAULT_JWKS_TTL_SECONDS,
        fetcher: JwksFetcher | None = None,
        min_refresh_seconds: int = DEFAULT_MIN_REFRESH_SECONDS,
        max_stale_seconds: int = DEFAULT_MAX_STALE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(url, str) or not url.strip():
            raise AuthConfigError("jwks_url_required")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._url = url.strip()
        self._ttl = max(1, int(ttl_seconds))
        self._min_refresh = max(0, int(min_refresh_seconds))
        self._max_stale = max(self._ttl, int(max_stale_seconds))
        self._fetcher: JwksFetcher = fetcher or _http_fetch
        self._clock = clock
        self._host = urlsplit(self._url).netloc or "unknown"
        self._lock = threading.Lock()
        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None
        self._last_attempt: float | None = None
        # 中文：未知 kid 的强制刷新单独计冷却，保证 IdP 轮换后首个请求即时生效，
        # 又不会被伪造 kid 打爆（每 min_refresh 至多一次）。
        self._last_forced: float | None = None

    @property
    def host(self) -> str:
        return self._host

    def get_key(self, kid: str | None) -> Any | None:
        """返回 kid 对应公钥；未知/歧义/缓存不可用返回 None（调用方转 401）。"""
        target = kid.strip() if isinstance(kid, str) and kid.strip() else None
        with self._lock:
            now = self._clock()
            self._drop_if_too_stale_locked(now)
            if self._fetched_at is None or now - self._fetched_at >= self._ttl:
                self._refresh_locked(now)
            key = self._lookup_locked(target)
            if key is None:
                # 中文：未知 kid 可能是 IdP 轮换；强制刷新一次（受独立冷却约束）。
                self._refresh_locked(now, force=True)
                key = self._lookup_locked(target)
            return key

    def _lookup_locked(self, kid: str | None) -> Any | None:
        if kid is not None:
            return self._keys.get(kid)
        # 中文：无 kid 仅当唯一键时放行；多键歧义 fail-closed。
        if len(self._keys) == 1:
            return next(iter(self._keys.values()))
        return None

    def _drop_if_too_stale_locked(self, now: float) -> None:
        if self._fetched_at is None or not self._keys:
            return
        if now - self._fetched_at > self._max_stale:
            logger.warning("auth.jwks_stale_dropped host=%s", self._host)
            self._keys = {}

    def _refresh_locked(self, now: float, *, force: bool = False) -> bool:
        if force:
            if self._last_forced is not None and now - self._last_forced < self._min_refresh:
                return False
            self._last_forced = now
        elif self._last_attempt is not None and now - self._last_attempt < self._min_refresh:
            return False
        self._last_attempt = now
        try:
            document = self._fetcher(self._url)
            keys = parse_jwks(document)
        except AuthConfigError as e:
            logger.warning("auth.jwks_refresh_failed host=%s code=%s", self._host, e.code)
            return False
        except (OSError, ValueError, TypeError, RuntimeError) as e:
            logger.warning("auth.jwks_refresh_failed host=%s error=%s", self._host, e)
            return False
        self._keys = keys
        self._fetched_at = now
        logger.info("auth.jwks_refreshed host=%s keys=%d", self._host, len(keys))
        return True


__all__ = ["JWKSCache", "parse_jwks"]

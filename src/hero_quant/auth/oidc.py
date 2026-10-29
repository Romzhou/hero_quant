"""auth.oidc — OIDC 配置装载与验签器组装（Keycloak 优先，标准 OIDC 兼容）。

职责：从 Settings（env 唯一入口）读取 issuer/audience/JWKS 地址与算法，构建 JWTVerifier。
架构位置：auth 包；deps/middleware 通过 default_verifier() 或 app.state 注入使用。
关键设计：
- 生产路径一律读 config.settings.get_settings()，auth 包不直读 os.environ；
- from_env(mapping) 仅保留给测试/显式注入，便于脱离 Settings 缓存验证解析逻辑；
- audience 必填：只校验签名不校验 aud 等于允许任意受众复用 token，明确拒绝；
- JWKS 地址缺省按 Keycloak 约定由 issuer 推导（{issuer}/protocol/openid-connect/certs）；
- 非 https issuer 仅告警不阻断（内部反代/测试场景），生产由部署层保证 TLS。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping

from hero_quant.auth.errors import AuthConfigError
from hero_quant.auth.jwks import DEFAULT_JWKS_TTL_SECONDS, JWKSCache, JwksFetcher
from hero_quant.auth.jwt import JWTConfig, JWTVerifier

logger = logging.getLogger(__name__)

DEFAULT_ALGORITHMS: tuple[str, ...] = ("RS256",)
DEFAULT_LEEWAY_SECONDS = 60
_KEYCLOAK_CERTS_PATH = "/protocol/openid-connect/certs"


class AuthMode(str, Enum):
    """AUTH_MODE 开关：off=迁移期放行（不解析身份），enforce=强制 Bearer。"""

    OFF = "off"
    ENFORCE = "enforce"


def _parse_auth_mode(raw: object) -> AuthMode:
    """模式归一：空/非法一律 enforce（安全默认），非法值告警。"""
    if raw is None or not str(raw).strip():
        return AuthMode.ENFORCE
    key = str(raw).strip().lower()
    if key == AuthMode.OFF.value:
        return AuthMode.OFF
    if key == AuthMode.ENFORCE.value:
        return AuthMode.ENFORCE
    logger.warning("auth.invalid_auth_mode value=%r fallback=enforce", raw)
    return AuthMode.ENFORCE


def get_auth_mode(env: Mapping[str, str] | None = None) -> AuthMode:
    """读取认证模式：默认走 Settings（env 唯一入口）；传 mapping 时用于测试。"""
    if env is None:
        from hero_quant.config.settings import get_settings

        return _parse_auth_mode(get_settings().auth_mode)
    return _parse_auth_mode(env.get("HERO_AUTH_MODE"))


def _clean(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _int_env(source: Mapping[str, str], key: str, default: int, *, low: int, high: int) -> int:
    """有界整数环境变量：非法/越界告警后回到默认值。"""
    raw = source.get(key)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("auth.invalid_int_env key=%s value=%r default=%d", key, raw, default)
        return default
    if value < low or value > high:
        logger.warning("auth.int_env_out_of_range key=%s value=%d range=[%d,%d]", key, value, low, high)
        return default
    return value


@dataclass(frozen=True)
class OIDCSettings:
    """OIDC 校验参数；issuer/audience/jwks_url 三项必填。"""

    issuer: str
    audience: str
    jwks_url: str
    algorithms: tuple[str, ...] = DEFAULT_ALGORITHMS
    leeway_seconds: int = DEFAULT_LEEWAY_SECONDS
    jwks_ttl_seconds: int = DEFAULT_JWKS_TTL_SECONDS
    mode: AuthMode = AuthMode.ENFORCE

    def __post_init__(self) -> None:
        issuer = _clean(self.issuer)
        audience = _clean(self.audience)
        jwks_url = _clean(self.jwks_url)
        if not issuer.startswith(("https://", "http://")):
            raise AuthConfigError("HERO_OIDC_ISSUER must be an http(s) URL")
        if not audience:
            raise AuthConfigError("HERO_OIDC_AUDIENCE is required")
        if not jwks_url.startswith(("https://", "http://")):
            raise AuthConfigError("HERO_OIDC_JWKS_URL must be an http(s) URL")
        if issuer.startswith("http://"):
            logger.warning("auth.oidc_issuer_without_tls issuer=%s", issuer)
        algorithms = tuple(str(alg).strip().upper() for alg in self.algorithms if str(alg).strip())
        object.__setattr__(self, "issuer", issuer)
        object.__setattr__(self, "audience", audience)
        object.__setattr__(self, "jwks_url", jwks_url)
        object.__setattr__(self, "algorithms", algorithms or DEFAULT_ALGORITHMS)

    @classmethod
    def from_settings(cls, settings: Any) -> "OIDCSettings":
        """从 config.settings.Settings 构造（生产路径；env 解析与校验已在网关完成）。"""
        issuer = _clean(getattr(settings, "oidc_issuer", ""))
        if not issuer:
            raise AuthConfigError("HERO_OIDC_ISSUER is required")
        audience = _clean(getattr(settings, "oidc_audience", ""))
        if not audience:
            raise AuthConfigError("HERO_OIDC_AUDIENCE is required")
        jwks_url = _clean(getattr(settings, "oidc_jwks_url", "")) or (issuer.rstrip("/") + _KEYCLOAK_CERTS_PATH)
        algorithms = tuple(getattr(settings, "oidc_algorithms", DEFAULT_ALGORITHMS) or DEFAULT_ALGORITHMS)
        return cls(
            issuer=issuer,
            audience=audience,
            jwks_url=jwks_url,
            algorithms=algorithms,
            leeway_seconds=int(getattr(settings, "oidc_leeway_seconds", DEFAULT_LEEWAY_SECONDS)),
            jwks_ttl_seconds=int(getattr(settings, "oidc_jwks_ttl_seconds", DEFAULT_JWKS_TTL_SECONDS)),
            mode=_parse_auth_mode(getattr(settings, "auth_mode", AuthMode.ENFORCE.value)),
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "OIDCSettings":
        """构造配置：env=None 走 Settings 网关；传 mapping 时按显式映射解析（测试用）。"""
        if env is None:
            from hero_quant.config.settings import get_settings

            return cls.from_settings(get_settings())
        issuer = _clean(env.get("HERO_OIDC_ISSUER"))
        if not issuer:
            raise AuthConfigError("HERO_OIDC_ISSUER is required")
        audience = _clean(env.get("HERO_OIDC_AUDIENCE"))
        if not audience:
            raise AuthConfigError("HERO_OIDC_AUDIENCE is required")
        jwks_url = _clean(env.get("HERO_OIDC_JWKS_URL")) or (issuer.rstrip("/") + _KEYCLOAK_CERTS_PATH)
        raw_algorithms = _clean(env.get("HERO_OIDC_ALGORITHMS"))
        algorithms = tuple(part.strip() for part in raw_algorithms.split(",") if part.strip()) if raw_algorithms else DEFAULT_ALGORITHMS
        return cls(
            issuer=issuer,
            audience=audience,
            jwks_url=jwks_url,
            algorithms=algorithms,
            leeway_seconds=_int_env(env, "HERO_OIDC_LEEWAY_SECONDS", DEFAULT_LEEWAY_SECONDS, low=0, high=600),
            jwks_ttl_seconds=_int_env(env, "HERO_OIDC_JWKS_TTL_SECONDS", DEFAULT_JWKS_TTL_SECONDS, low=30, high=3600),
            mode=get_auth_mode(env),
        )


def auth_enabled(env: Mapping[str, str] | None = None) -> bool:
    """接线判据：模式未关闭且 issuer/audience 均已配置时才挂载强制认证。

    中文：默认走 Settings 网关；HERO_AUTH_MODE 未设时为 enforce（安全默认），因此这里
    额外要求 OIDC 配置齐全，避免开发/预发因缺配置被中间件 503 打穿。
    """
    if env is None:
        from hero_quant.config.settings import get_settings

        settings = get_settings()
        if _parse_auth_mode(settings.auth_mode) is AuthMode.OFF:
            return False
        return bool(_clean(settings.oidc_issuer) and _clean(settings.oidc_audience))
    if get_auth_mode(env) is AuthMode.OFF:
        return False
    return bool(_clean(env.get("HERO_OIDC_ISSUER")) and _clean(env.get("HERO_OIDC_AUDIENCE")))


def build_verifier(
    settings: OIDCSettings,
    *,
    fetcher: JwksFetcher | None = None,
    token_clock: Callable[[], float] = time.time,
    cache_clock: Callable[[], float] = time.monotonic,
) -> JWTVerifier:
    """组装 JWKS 缓存 + 验签器；两个时钟分离（token 用 epoch，缓存用 monotonic）。"""
    cache = JWKSCache(settings.jwks_url, ttl_seconds=settings.jwks_ttl_seconds, fetcher=fetcher, clock=cache_clock)
    config = JWTConfig(
        issuer=settings.issuer,
        audience=settings.audience,
        algorithms=settings.algorithms,
        leeway_seconds=settings.leeway_seconds,
    )
    return JWTVerifier(cache.get_key, config, clock=token_clock)


__all__ = ["AuthMode", "OIDCSettings", "auth_enabled", "build_verifier", "get_auth_mode"]

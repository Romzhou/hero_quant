"""PR-151 Redis DSN 口令注入 —— 与 `settings._apply_pg_password` 同规则。

规则（保守，全部显式）：
- 非 `redis://` / `rediss://` scheme 不动；
- DSN 已带口令 → 显式优先，不动；
- 无 userinfo（netloc 里没有 `@`）→ 不动；
  `redis://:pass@host` 的空用户名形态属于**已带口令**，不会被改写；反过来
  `redis://@host` 算「有 userinfo、无口令」，注入后得到标准的`:pass@` 口令-only 形态；
- provider 取不到口令（未配置 / vault / kms 未实现）→ 不动，**绝不回落 os.environ**；
- 口令按 RFC 3986 百分号编码后写入，`@ : / %` 等保留字符不会撕开 DSN。

日志只记 host，永不记口令或完整 DSN。本模块不直读 env：口令统一经
`secrets.resolve.resolve_secret`（逻辑名 `redis_password`，已在 env_provider 登记）。
接线（settings.`_redis_dsn_from_env` 调用本函数）由主控在批次 4 完成。
"""

from __future__ import annotations

import logging
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .resolve import resolve_secret

logger = logging.getLogger(__name__)

#: 逻辑名（唯一登记处：env_provider.SECRET_ENV_VARS → HERO_REDIS_PASSWORD）
REDIS_PASSWORD_NAME = "redis_password"

#: 只有这两种 scheme 会被注入；unix:// 没有 userinfo 语义，天然不命中。
REDIS_SCHEMES = ("redis", "rediss")


def _password_from_provider() -> str | None:
    """口令经 secrets provider 取（env 后端即 HERO_REDIS_PASSWORD）。

    vault/kms 未实现时 resolve_secret 返回 reason 而非回落 env —— DSN 保持无口令，
    连接失败按既有语义暴露（/ready 如实 503），不偷偷用明文。
    """
    value, reason = resolve_secret(REDIS_PASSWORD_NAME)
    if reason:
        logger.warning("secrets.redis_password_unavailable error_code=%s", reason)
    return value or None


def apply_redis_password(dsn: str | None) -> str | None:
    """给「有 userinfo 但没口令」的 Redis DSN 注入 provider 提供的口令。"""
    if not dsn or not isinstance(dsn, str):
        return dsn
    try:
        parts = urlsplit(dsn)
    except ValueError:
        return dsn
    if parts.scheme.lower() not in REDIS_SCHEMES:
        return dsn
    if "@" not in parts.netloc:
        return dsn
    if parts.password:
        return dsn
    password = _password_from_provider()
    if not password:
        return dsn
    try:
        port = parts.port
    except ValueError:
        logger.warning("secrets.redis_password_injected_skipped error_code=invalid_port")
        return dsn
    user = unquote(parts.username or "")
    host = parts.hostname or ""
    host_part = f"[{host}]" if ":" in host else host
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host_part}"
    if port:
        netloc += f":{port}"
    logger.info("secrets.redis_password_injected host=%s", host)
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


__all__ = ["REDIS_PASSWORD_NAME", "REDIS_SCHEMES", "apply_redis_password"]

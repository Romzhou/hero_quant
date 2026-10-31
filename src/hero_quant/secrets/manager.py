"""PR-151 密钥后端选择 —— 唯一入口，fail-closed。

规则（与 `HERO_LLM_BASE_URL` 校验风格一致，不静默回落）：
- `env`（默认）→ `EnvSecretsProvider`，可用；
- `vault` / `kms` → **显式不可用**：抛 `UnsupportedSecretsBackendError`，
  绝不静默回落到 env（回落到明文 .env 等于谎报"已托管"）；
- 其它未知值 → `ValueError`（settings 层构造即失败）。
"""

from __future__ import annotations

from hero_quant.config.settings import secrets_backend_from_env

from .base import SecretsProvider
from .env_provider import EnvSecretsProvider

ENV_BACKEND = "env"
# 本批只登记接口、不实现：命中即抛 UnsupportedSecretsBackendError
RESERVED_BACKENDS: tuple[str, ...] = ("vault", "kms")
KNOWN_BACKENDS: tuple[str, ...] = (ENV_BACKEND,) + RESERVED_BACKENDS


class UnsupportedSecretsBackendError(RuntimeError):
    """后端已登记但未在本构建中实现（Vault/KMS）。不提供 env 静默回落。"""


def get_secrets_provider(backend: str | None = None) -> SecretsProvider:
    """按后端名取 provider。`backend=None` 时读 `HERO_SECRETS_BACKEND`（默认 env）。"""
    raw = backend if backend is not None else secrets_backend_from_env()
    name = (raw or "").strip().lower() or ENV_BACKEND
    if name == ENV_BACKEND:
        return EnvSecretsProvider()
    if name in RESERVED_BACKENDS:
        raise UnsupportedSecretsBackendError(
            f"secrets backend {name!r} is not implemented in this build; "
            f"refusing to silently fall back to {ENV_BACKEND!r} (fail-closed). "
            f"See docs/SECURITY-SECRETS.md §未覆盖范围"
        )
    raise ValueError(f"unknown secrets backend {name!r}; expected one of {sorted(KNOWN_BACKENDS)}")

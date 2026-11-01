"""PR-151 密钥托管（Phase 1）—— provider 抽象 + env 后端 + 轮换记账。

对外最小面：
    from hero_quant.secrets import get_secrets_provider, begin_rotation, complete_rotation, rotation_status

本批**没有**真 Vault/KMS 后端：`vault` / `kms` 命中即抛
`UnsupportedSecretsBackendError`，不静默回落 env。生产 `.env` 仍是明文，
详见 `docs/SECURITY-SECRETS.md` §未覆盖范围。
"""

from __future__ import annotations

from .base import SLOT_CURRENT, SLOT_NEXT, SecretValue, SecretsProvider, fingerprint
from .env_provider import NEXT_SUFFIX, SECRET_ENV_VARS, EnvSecretsProvider
from .manager import (
    ENV_BACKEND,
    KNOWN_BACKENDS,
    RESERVED_BACKENDS,
    UnsupportedSecretsBackendError,
    get_secrets_provider,
)
from .resolve import (LLM_KEY_NAME, OPENAI_KEY_NAME, PROVIDER_KEY_NAMES, resolve_llm_api_key, resolve_secret, secret_value)
from .rotation import (
    DEFAULT_STATE_PATH,
    STATE_KEYS,
    STATE_ROTATING,
    STATE_STABLE,
    STATE_UNKNOWN,
    RotationStore,
    SecretRotationError,
    begin_rotation,
    complete_rotation,
    rotation_status,
)

__all__ = [
    "ENV_BACKEND",
    "KNOWN_BACKENDS",
    "NEXT_SUFFIX",
    "RESERVED_BACKENDS",
    "SECRET_ENV_VARS",
    "STATE_KEYS",
    "STATE_ROTATING",
    "STATE_STABLE",
    "STATE_UNKNOWN",
    "SLOT_CURRENT",
    "SLOT_NEXT",
    "DEFAULT_STATE_PATH",
    "EnvSecretsProvider",
    "RotationStore",
    "SecretRotationError",
    "SecretValue",
    "SecretsProvider",
    "UnsupportedSecretsBackendError",
    "begin_rotation",
    "complete_rotation",
    "fingerprint",
    "get_secrets_provider",
    "rotation_status",
    "LLM_KEY_NAME",
    "OPENAI_KEY_NAME",
    "PROVIDER_KEY_NAMES",
    "resolve_llm_api_key",
    "resolve_secret",
    "secret_value",
]

"""业务侧取密钥的唯一入口（PR-151 接线）。

设计约束（与 AGENTS §6 fail-closed 对齐）：

1. **单一入口**：业务代码不再直接 `os.environ.get("HERO_API_KEY")` / 读 `Settings.api_key`；
   统一走本模块，后端由 `HERO_SECRETS_BACKEND` 决定。
2. **不静默回落**：后端是 `vault`/`kms`（登记未实现）时返回 `(None, "secrets_backend_unavailable")`，
   **绝不**改成读 env / Settings 字段——那条路等于对外谎报"密钥已托管"。
3. **缺失即缺失**：密钥未配置返回 `(None, None)`，由调用方按既有语义降级
   （`LLMFactory` 非 strict → `_FakeLLM`；strict → `ValueError`）。
4. **不落值**：日志只带逻辑名与 `error_code`，密钥值永不进日志/异常消息。

兼容性说明：`Settings` 字段（`api_key`/`openai_api_key`/`cohere_api_key`）仍作为 env 后端下的
次选来源——它们本来就来自同一批环境变量，保留是为了不改既有注入式单测的行为。仅在
**env 后端**下作为回退；非 env 后端一律不读。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 逻辑名（唯一登记处见 env_provider.SECRET_ENV_VARS）
LLM_KEY_NAME = "llm_api_key"
OPENAI_KEY_NAME = "openai_api_key"
COHERE_KEY_NAME = "cohere_api_key"

# provider → 依次尝试的逻辑密钥名（与 llm/gateway.py 的链一致）
PROVIDER_KEY_NAMES: dict[str, tuple[str, ...]] = {
    "openai": (LLM_KEY_NAME, OPENAI_KEY_NAME),
    "deepseek": ("deepseek_api_key",),
    "anthropic": ("anthropic_api_key", "claude_api_key"),
}

_BACKEND_REASONS = {
    "UnsupportedSecretsBackendError": "secrets_backend_unavailable",
    "ValueError": "secrets_backend_invalid",
}


def _get_provider() -> tuple[Any | None, str | None]:
    """取 provider；后端不可用/非法时返回 (None, reason)，并记结构化 warning。"""
    from .manager import UnsupportedSecretsBackendError, get_secrets_provider

    try:
        return get_secrets_provider(), None
    except UnsupportedSecretsBackendError as e:
        logger.warning(
            "secrets.backend_unavailable error_code=secrets_backend_unavailable detail=%s",
            str(e),
        )
        return None, _BACKEND_REASONS["UnsupportedSecretsBackendError"]
    except ValueError as e:
        logger.warning("secrets.backend_invalid error_code=secrets_backend_invalid detail=%s", str(e))
        return None, _BACKEND_REASONS["ValueError"]


def secret_value(name: str) -> tuple[str | None, str | None]:
    """按逻辑名取密钥 → (值, reason)。未配置返回 (None, None)；后端不可用返回 (None, reason)。"""
    provider, reason = _get_provider()
    if provider is None:
        return None, reason
    try:
        sv = provider.get_secret(name)
    except (ValueError, TypeError, AttributeError, OSError, RuntimeError) as e:
        logger.warning("secrets.provider_error error_code=secrets_provider_error name=%s detail=%s", name, str(e))
        return None, "secrets_provider_error"
    if sv is None:
        return None, None
    try:
        val = sv.reveal()
    except (AttributeError, ValueError, TypeError):
        logger.warning("secrets.value_invalid error_code=secrets_value_invalid name=%s", name)
        return None, "secrets_value_invalid"
    return (val, None) if val else (None, None)


def resolve_secret(name: str, legacy: str | None = None) -> tuple[str | None, str | None]:
    """provider 优先；仅在 env 后端且 provider 无值时，才用 `legacy`（Settings 同源字段）兜底。"""
    val, reason = secret_value(name)
    if val:
        return val, None
    if reason:
        return None, reason
    legacy_val = legacy.strip() if isinstance(legacy, str) else ""
    if legacy_val:
        return legacy_val, None
    return None, None


def resolve_llm_api_key(
    settings: Any | None = None,
    *,
    provider: str | None = None,
    explicit: str | None = None,
) -> tuple[str | None, str | None]:
    """LLM key 解析（顺序与历史实现一致，只换成经 provider 取）。

    顺序：显式入参 → HERO_API_KEY → Settings.api_key → OPENAI_API_KEY →
    Settings.openai_api_key → provider 专属（deepseek/anthropic）→ 无。
    任一环节发现后端不可用（reason 非空）立即返回，不再往下回落。
    """
    if explicit:
        return explicit, None
    names = (LLM_KEY_NAME, OPENAI_KEY_NAME)
    for name in names:
        val, reason = secret_value(name)
        if val:
            return val, None
        if reason:
            return None, reason
        legacy = None
        if settings is not None:
            legacy = getattr(settings, "api_key", None) if name == LLM_KEY_NAME else getattr(settings, "openai_api_key", None)
        legacy_val = legacy.strip() if isinstance(legacy, str) else ""
        if legacy_val:
            return legacy_val, None
    for name in PROVIDER_KEY_NAMES.get(str(provider or "openai"), ()):
        if name in names:
            continue
        val, reason = secret_value(name)
        if val:
            return val, None
        if reason:
            return None, reason
    return None, None

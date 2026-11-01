"""PR-151 接线：业务侧取密钥必须经 secrets provider，且 vault/kms 不回落 env。

覆盖：
- env 后端下 LLM key 的完整优先级（显式 > HERO_API_KEY > Settings.api_key > OPENAI_API_KEY > provider 专属）；
- vault/kms 后端：返回 (None, secrets_backend_unavailable)，**不**回落 env 或 Settings 字段；
- LLMFactory strict / 非 strict 两条降级语义；
- LLMGateway 的 primary/回退链不再直读 os.environ；
- embed / rerank 的 key 也走同一入口；
- 日志里不出现密钥真值。
"""

from __future__ import annotations

import logging

import pytest

from hero_quant.config.settings import Settings
from hero_quant.llm.client import LLMClient
from hero_quant.llm.factory import LLMFactory
from hero_quant.llm.gateway import LLMGateway
from hero_quant.secrets.resolve import resolve_llm_api_key, resolve_secret, secret_value

_KEY_ENVS = (
    "HERO_SECRETS_BACKEND",
    "HERO_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "ANTHROPIC_API_KEY",
    "CLAUDE_API_KEY",
    "COHERE_API_KEY",
)
_SECRET = "sk-wiring-0000000000000000"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    yield


def _settings(provider: str = "openai", api_key: str | None = None, openai_api_key: str | None = None) -> Settings:
    return Settings(
        llm_provider=provider,
        llm_model="gpt-4o-mini",
        llm_model_deep="gpt-4o-mini",
        llm_model_quick="gpt-4o-mini",
        api_key=api_key,
        openai_api_key=openai_api_key,
    )


# ---------------------------------------------------------------- env 后端优先级


def test_env_backend_reads_hero_api_key(monkeypatch):
    monkeypatch.setenv("HERO_API_KEY", _SECRET)
    assert resolve_llm_api_key(None) == (_SECRET, None)
    assert secret_value("llm_api_key") == (_SECRET, None)


def test_explicit_argument_wins(monkeypatch):
    monkeypatch.setenv("HERO_API_KEY", _SECRET)
    assert resolve_llm_api_key(None, explicit="explicit-key") == ("explicit-key", None)


def test_settings_field_is_used_only_as_env_backend_fallback(monkeypatch):
    # env 没有 key，env 后端下回退 Settings（同源字段，历史行为不变）
    monkeypatch.setenv("HERO_SECRETS_BACKEND", "env")
    assert resolve_llm_api_key(_settings(api_key="settings-key")) == ("settings-key", None)


def test_openai_env_is_second_choice(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-env-key")
    assert resolve_llm_api_key(_settings()) == ("openai-env-key", None)


@pytest.mark.parametrize(
    ("provider", "env_name"),
    [("deepseek", "DEEPSEEK_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY"), ("anthropic", "CLAUDE_API_KEY")],
)
def test_provider_specific_env_keys(monkeypatch, provider, env_name):
    monkeypatch.setenv(env_name, "provider-specific-key")
    assert resolve_llm_api_key(_settings(provider), provider=provider) == ("provider-specific-key", None)


def test_missing_key_returns_none_none(monkeypatch):
    assert resolve_llm_api_key(_settings()) == (None, None)


# ---------------------------------------------------------------- 非 env 后端：不回落


@pytest.mark.parametrize("backend", ["vault", "kms"])
def test_unimplemented_backend_does_not_fall_back_to_env(monkeypatch, backend):
    monkeypatch.setenv("HERO_SECRETS_BACKEND", backend)
    monkeypatch.setenv("HERO_API_KEY", _SECRET)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-env-key")
    assert resolve_llm_api_key(_settings(api_key="settings-key", openai_api_key="settings-oa")) == (
        None,
        "secrets_backend_unavailable",
    )


def test_unknown_backend_is_fail_closed(monkeypatch):
    monkeypatch.setenv("HERO_SECRETS_BACKEND", "no-such-backend")
    monkeypatch.setenv("HERO_API_KEY", _SECRET)
    assert resolve_llm_api_key(None) == (None, "secrets_backend_invalid")


# ---------------------------------------------------------------- factory / gateway 行为


def test_factory_strict_raises_with_reason_when_backend_unavailable(monkeypatch):
    monkeypatch.setenv("HERO_SECRETS_BACKEND", "vault")
    monkeypatch.setenv("HERO_API_KEY", _SECRET)
    factory = LLMFactory(_settings())
    factory.strict = True
    with pytest.raises(ValueError) as ei:
        factory.create()
    msg = str(ei.value)
    assert "api_key missing" in msg
    assert "secrets_backend_unavailable" in msg
    assert _SECRET not in msg  # 真值永不进异常消息


def test_factory_non_strict_still_builds_client_when_backend_unavailable(monkeypatch):
    monkeypatch.setenv("HERO_SECRETS_BACKEND", "vault")
    monkeypatch.setenv("HERO_API_KEY", _SECRET)
    llm = LLMFactory(_settings()).create()
    assert isinstance(llm, LLMClient)


def test_gateway_does_not_read_env_when_backend_unavailable(monkeypatch):
    monkeypatch.setenv("HERO_SECRETS_BACKEND", "vault")
    monkeypatch.setenv("HERO_API_KEY", _SECRET)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-env-key")
    gw = LLMGateway.from_settings(_settings())
    assert gw.chain == ["openai"]  # 没有可用 key → 只有 primary 单链，且不回退 env


def test_gateway_primary_key_comes_from_secret_provider(monkeypatch):
    monkeypatch.setenv("HERO_API_KEY", _SECRET)
    gw = LLMGateway.from_settings(_settings())
    assert gw.chain[0] == "openai"
    assert gw._states[0].spec.api_key == _SECRET  # noqa: SLF001 — 断言实际注入的 key


# ---------------------------------------------------------------- embed / rerank


def test_resolve_secret_covers_cohere_and_reports_reason(monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "cohere-env-key")
    assert resolve_secret("cohere_api_key", "") == ("cohere-env-key", None)
    monkeypatch.setenv("HERO_SECRETS_BACKEND", "vault")
    assert resolve_secret("cohere_api_key", "settings-cohere") == (None, "secrets_backend_unavailable")


def test_rerank_uses_provider_and_degrades_to_empty_on_vault(monkeypatch):
    from hero_quant.memory.rerank import CohereReranker

    monkeypatch.setenv("HERO_SECRETS_BACKEND", "vault")
    monkeypatch.setenv("COHERE_API_KEY", "cohere-env-key")
    assert CohereReranker().api_key == ""


def test_embed_returns_none_when_backend_unavailable(monkeypatch):
    from hero_quant.agent.embed import _try_openai

    monkeypatch.setenv("HERO_SECRETS_BACKEND", "vault")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-env-key")
    assert _try_openai("hello", 8) is None


# ---------------------------------------------------------------- 日志卫生


def test_secret_value_never_logged(monkeypatch, caplog):
    monkeypatch.setenv("HERO_SECRETS_BACKEND", "vault")
    monkeypatch.setenv("HERO_API_KEY", _SECRET)
    with caplog.at_level(logging.DEBUG):
        resolve_llm_api_key(None)
    assert _SECRET not in caplog.text
    assert "secrets_backend_unavailable" in caplog.text  # 但失败原因必须可见

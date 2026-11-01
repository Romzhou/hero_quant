"""Model-selection factory with multi-provider routing (openai/deepseek/anthropic) and LLMClient creation."""

from __future__ import annotations

import os
from typing import Mapping

from hero_quant.config.settings import Settings
from hero_quant.secrets.resolve import resolve_llm_api_key

from .catalog import DEFAULT_MODEL, ModelInfo, UnknownModelError, resolve_model

_ALLOWED_PROVIDERS = {"openai", "deepseek", "anthropic"}


def _normalize_provider(raw: str | None, *, strict: bool = False) -> str:
    if not raw:
        return "openai"
    p = str(raw).strip().lower()
    if p in _ALLOWED_PROVIDERS:
        return p
    if "deepseek" in p:
        return "deepseek"
    if "anthropic" in p or "claude" in p:
        return "anthropic"
    if "openai" in p or "gpt" in p:
        return "openai"
    if strict:
        raise ValueError(f"unknown llm_provider: {raw!r}")
    import logging as _logging

    _logging.getLogger(__name__).warning("unknown llm_provider %r, falling back to openai", raw)
    return "openai"


class _FallbackChat:
    """Minimal chat for offline/testing when provider SDK not installed."""

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def stream_chat(self, prompt: str, timeout: int | None = None):
        yield f"[{self.model}] {prompt[:80]}"

    def invoke(self, prompt: str):
        return f"[{self.model}] {prompt[:80]}"

    def chat(self, prompt: str):
        return self.invoke(prompt)

    def __call__(self, prompt: str):
        return self.invoke(prompt)


STAGE_TO_SLOT: Mapping[str, str] = {
    "plan": "deep",
    "verify": "deep",
    "tool_summary": "quick",
    "tool_debate": "quick",
    "summary": "quick",
    "debate": "quick",
}


def slot_for_stage(stage: str) -> str:
    """Map a logical execution stage to the configured model slot."""
    key = str(stage or "").strip().lower().replace("-", "_").replace(" ", "_")
    try:
        return STAGE_TO_SLOT[key]
    except KeyError as exc:
        raise ValueError(f"unknown LLM stage: {stage}") from exc


class LLMFactory:
    """Resolve configured model metadata and route provider to LLMClient."""

    def __init__(self, settings: Settings | None = None, *, strict: bool = False):
        self.settings = settings or Settings()
        self.strict = strict

    @property
    def provider(self) -> str:
        raw = getattr(self.settings, "llm_provider", "openai")
        return _normalize_provider(raw, strict=self.strict)

    def slot_for_stage(self, stage: str) -> str:
        return slot_for_stage(stage)

    def model_for_stage(self, stage: str) -> ModelInfo:
        slot = self.slot_for_stage(stage)
        requested = getattr(self.settings, f"llm_model_{slot}", self.settings.llm_model)
        return resolve_model(
            requested,
            fallback=self.settings.llm_model or DEFAULT_MODEL,
            strict=self.strict,
        )

    def create(
        self,
        model: str | None = None,
        api_key: str | None = None,
        provider: str | None = None,
        base_url: str | None = None,
        **kwargs,
    ):
        """Create an LLMClient routed by provider with timeout=30.

        `provider` / `base_url` 为显式覆盖项（供 `llm/gateway.py` 构造降级链时逐 provider 建客户端），
        不传时行为与历史完全一致（读 `settings.llm_provider`）。
        """

        # defer import to avoid circular
        import logging as _logging

        from .client import LLMClient

        provider = _normalize_provider(provider, strict=self.strict) if provider else self.provider
        # resolve model — strict mode propagates UnknownModelError
        if model is None:
            if self.strict:
                model = self.model_for_stage("plan").name
            else:
                try:
                    model = self.model_for_stage("plan").name
                except (ValueError, UnknownModelError) as e:
                    _logging.getLogger(__name__).warning("model_for_stage failed, fallback to settings.llm_model: %s", e)
                    model = getattr(self.settings, "llm_model", DEFAULT_MODEL) or DEFAULT_MODEL
        # resolve api key — 统一走 secrets provider（PR-151 接线）：
        # provider 优先，env 后端下才回退 Settings 同源字段；vault/kms 不可用时不回落。
        key, key_reason = resolve_llm_api_key(self.settings, provider=provider, explicit=api_key)
        if not key:
            if self.strict:
                raise ValueError(
                    "LLM api_key missing; configure api_key / OPENAI_API_KEY / provider-specific key"
                    + (f" ({key_reason})" if key_reason else "")
                )
            _logging.getLogger(__name__).warning(
                "LLM api_key missing, using dummy key for offline fallback reason=%s", key_reason or "not_configured"
            )
            key = "test"
        # provider-specific chat creation
        chat = None
        # common kwargs — pop timeout to prevent duplicate-keyword TypeError
        temperature = kwargs.pop("temperature", 0.2)
        streaming = kwargs.pop("streaming", True)
        timeout = kwargs.pop("timeout", 30)
        # try real SDK, fallback to _FallbackChat only on ImportError (strict propagates)
        if provider == "openai":
            try:
                from langchain_openai import ChatOpenAI

                chat = ChatOpenAI(model=model, api_key=key, base_url=base_url, streaming=streaming, temperature=temperature, timeout=timeout, **kwargs)
            except ImportError as e:
                if self.strict:
                    raise
                _logging.getLogger(__name__).warning("ChatOpenAI not installed, using fallback: %s", e)
                chat = _FallbackChat(model=model)
            except Exception as e:
                _logging.getLogger(__name__).warning("ChatOpenAI init failed: %s", e)
                if self.strict:
                    raise
                chat = _FallbackChat(model=model)
        elif provider == "deepseek":
            try:
                from langchain_openai import ChatOpenAI

                base_url = base_url or os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com")
                chat = ChatOpenAI(model=model, api_key=key, base_url=base_url, streaming=streaming, temperature=temperature, timeout=timeout, **kwargs)
            except ImportError as e:
                if self.strict:
                    raise
                _logging.getLogger(__name__).warning("ChatOpenAI for deepseek not installed: %s", e)
                chat = _FallbackChat(model=model)
            except Exception as e:
                _logging.getLogger(__name__).warning("DeepSeek Chat init failed: %s", e)
                if self.strict:
                    raise
                chat = _FallbackChat(model=model)
        elif provider == "anthropic":
            try:
                from langchain_anthropic import ChatAnthropic  # type: ignore

                chat = ChatAnthropic(model=model, api_key=key, streaming=streaming, temperature=temperature, timeout=timeout, **({"base_url": base_url} if base_url else {}), **kwargs)  # type: ignore
            except ImportError:
                # fallback to OpenAI-compatible if anthropic SDK missing
                try:
                    from langchain_openai import ChatOpenAI

                    chat = ChatOpenAI(model=model, api_key=key, base_url=base_url, streaming=streaming, temperature=temperature, timeout=timeout, **kwargs)
                except ImportError as e:
                    if self.strict:
                        raise
                    _logging.getLogger(__name__).warning("no chat SDK installed: %s", e)
                    chat = _FallbackChat(model=model)
                except Exception as e:
                    if self.strict:
                        raise
                    _logging.getLogger(__name__).warning("fallback ChatOpenAI failed: %s", e)
                    chat = _FallbackChat(model=model)
            except Exception as e:
                _logging.getLogger(__name__).warning("ChatAnthropic init failed: %s", e)
                if self.strict:
                    raise
                chat = _FallbackChat(model=model)
        else:
            chat = _FallbackChat(model=model)

        # ensure timeout attribute for introspection (fail-visible)
        try:
            if not hasattr(chat, "timeout"):
                setattr(chat, "timeout", timeout)
        except Exception as e:
            _logging.getLogger(__name__).debug("could not set timeout on chat object %r error_code=llm_chat_timeout_attr_failed: %s", type(chat), e)  # intentional: 内省属性 garnish；超时透传由 _invoke_once 签名探测保证
        return LLMClient(chat, timeout=timeout, max_retries=3)


def model_for_stage(stage: str, settings: Settings | None = None, *, strict: bool = False) -> ModelInfo:
    """Resolve one stage without creating an LLM client."""
    return LLMFactory(settings, strict=strict).model_for_stage(stage)


__all__ = ["LLMFactory", "STAGE_TO_SLOT", "model_for_stage", "slot_for_stage"]

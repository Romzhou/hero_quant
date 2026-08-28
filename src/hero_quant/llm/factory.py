"""Model-selection factory with multi-provider routing (openai/deepseek/anthropic) and LLMClient creation."""

from __future__ import annotations

import os
from typing import Mapping

from hero_quant.config.settings import Settings

from .catalog import DEFAULT_MODEL, ModelInfo, resolve_model

_ALLOWED_PROVIDERS = {"openai", "deepseek", "anthropic"}


def _normalize_provider(raw: str | None) -> str:
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
        return _normalize_provider(raw)

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

    def create(self, model: str | None = None, api_key: str | None = None, **kwargs):
        """Create an LLMClient routed by provider with timeout=30."""

        # defer import to avoid circular
        from .client import LLMClient

        provider = self.provider
        # resolve model
        if model is None:
            try:
                model = self.model_for_stage("plan").name
            except Exception:
                model = getattr(self.settings, "llm_model", DEFAULT_MODEL) or DEFAULT_MODEL
        key = api_key or getattr(self.settings, "api_key", None) or getattr(self.settings, "openai_api_key", None) or os.environ.get("OPENAI_API_KEY") or os.environ.get("HERO_API_KEY") or "test"
        # provider-specific chat creation
        chat = None
        # common kwargs
        temperature = kwargs.pop("temperature", 0.2)
        streaming = kwargs.pop("streaming", True)
        # try real SDK, fallback to _FallbackChat
        if provider == "openai":
            try:
                from langchain_openai import ChatOpenAI

                chat = ChatOpenAI(model=model, api_key=key, streaming=streaming, temperature=temperature, timeout=30, **kwargs)
            except Exception:
                chat = _FallbackChat(model=model)
        elif provider == "deepseek":
            try:
                from langchain_openai import ChatOpenAI

                base_url = os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com")
                # deepseek uses openai-compatible
                chat = ChatOpenAI(model=model, api_key=key, base_url=base_url, streaming=streaming, temperature=temperature, timeout=30, **kwargs)
            except Exception:
                chat = _FallbackChat(model=model)
        elif provider == "anthropic":
            try:
                from langchain_anthropic import ChatAnthropic  # type: ignore

                chat = ChatAnthropic(model=model, api_key=key, streaming=streaming, temperature=temperature, timeout=30, **kwargs)  # type: ignore
            except Exception:
                try:
                    from langchain_openai import ChatOpenAI

                    chat = ChatOpenAI(model=model, api_key=key, streaming=streaming, temperature=temperature, timeout=30, **kwargs)
                except Exception:
                    chat = _FallbackChat(model=model)
        else:
            chat = _FallbackChat(model=model)

        # ensure timeout attribute for introspection
        try:
            if not hasattr(chat, "timeout"):
                setattr(chat, "timeout", 30)
        except Exception:
            pass
        return LLMClient(chat, timeout=30, max_retries=3)


def model_for_stage(stage: str, settings: Settings | None = None, *, strict: bool = False) -> ModelInfo:
    """Resolve one stage without creating an LLM client."""
    return LLMFactory(settings, strict=strict).model_for_stage(stage)


__all__ = ["LLMFactory", "STAGE_TO_SLOT", "model_for_stage", "slot_for_stage"]

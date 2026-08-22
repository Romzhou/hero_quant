"""Small model-selection factory; it does not create clients or make network calls."""

from __future__ import annotations

from typing import Mapping

from hero_quant.config.settings import Settings

from .catalog import DEFAULT_MODEL, ModelInfo, resolve_model


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
    """Resolve configured model metadata for graph and tool execution stages."""

    def __init__(self, settings: Settings | None = None, *, strict: bool = False):
        self.settings = settings or Settings()
        self.strict = strict

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


def model_for_stage(stage: str, settings: Settings | None = None, *, strict: bool = False) -> ModelInfo:
    """Resolve one stage without creating an LLM client."""
    return LLMFactory(settings, strict=strict).model_for_stage(stage)


__all__ = ["LLMFactory", "STAGE_TO_SLOT", "model_for_stage", "slot_for_stage"]

"""LLM model catalog and stage-to-slot resolution without a network client."""

from .catalog import (
    DEFAULT_MODEL,
    MODEL_CATALOG,
    ModelInfo,
    UnknownModelError,
    get_model_info,
    list_models,
    resolve_model,
)
from .factory import LLMFactory, STAGE_TO_SLOT, model_for_stage, slot_for_stage

__all__ = [
    "DEFAULT_MODEL",
    "MODEL_CATALOG",
    "ModelInfo",
    "UnknownModelError",
    "get_model_info",
    "list_models",
    "resolve_model",
    "LLMFactory",
    "STAGE_TO_SLOT",
    "model_for_stage",
    "slot_for_stage",
]

"""Built-in LLM model metadata and safe model-name resolution."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


DEFAULT_MODEL = "gpt-4o-mini"


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Catalog metadata; prices are USD per one million tokens."""

    name: str
    input_price_per_million_tokens: float
    output_price_per_million_tokens: float
    capabilities: tuple[str, ...]
    context_window: int | None = None

    @property
    def input_price(self) -> float:
        """Short alias for callers rendering catalog pricing."""
        return self.input_price_per_million_tokens

    @property
    def output_price(self) -> float:
        """Short alias for callers rendering catalog pricing."""
        return self.output_price_per_million_tokens


MODEL_CATALOG: Mapping[str, ModelInfo] = MappingProxyType(
    {
        "gpt-4o-mini": ModelInfo(
            name="gpt-4o-mini",
            input_price_per_million_tokens=0.15,
            output_price_per_million_tokens=0.60,
            capabilities=("chat", "tool_calling", "structured_output"),
            context_window=128_000,
        ),
        "gpt-4o": ModelInfo(
            name="gpt-4o",
            input_price_per_million_tokens=2.50,
            output_price_per_million_tokens=10.00,
            capabilities=("chat", "tool_calling", "structured_output"),
            context_window=128_000,
        ),
        "gpt-4.1-mini": ModelInfo(
            name="gpt-4.1-mini",
            input_price_per_million_tokens=0.40,
            output_price_per_million_tokens=1.60,
            capabilities=("chat", "tool_calling", "structured_output"),
            context_window=1_000_000,
        ),
        "gpt-4.1": ModelInfo(
            name="gpt-4.1",
            input_price_per_million_tokens=2.00,
            output_price_per_million_tokens=8.00,
            capabilities=("chat", "tool_calling", "structured_output"),
            context_window=1_000_000,
        ),
    }
)


class UnknownModelError(ValueError):
    """Raised when strict resolution receives a model absent from the catalog."""


def _model_name(model: str | None) -> str:
    return str(model or "").strip()


def resolve_model(
    model: str | None = None,
    *,
    fallback: str = DEFAULT_MODEL,
    strict: bool = False,
) -> ModelInfo:
    """Resolve a model from the catalog, optionally failing instead of falling back."""
    requested = _model_name(model)
    if requested in MODEL_CATALOG:
        return MODEL_CATALOG[requested]
    if strict:
        raise UnknownModelError(f"unknown LLM model: {requested or '<empty>'}")

    fallback_name = _model_name(fallback)
    if fallback_name in MODEL_CATALOG:
        return MODEL_CATALOG[fallback_name]
    return MODEL_CATALOG[DEFAULT_MODEL]


def get_model_info(model: str, *, strict: bool = False, fallback: str = DEFAULT_MODEL) -> ModelInfo:
    """Return catalog metadata for a model, using the same safe resolution rules."""
    return resolve_model(model, fallback=fallback, strict=strict)


def list_models() -> tuple[str, ...]:
    """Return catalog model identifiers in deterministic order."""
    return tuple(sorted(MODEL_CATALOG))


__all__ = [
    "DEFAULT_MODEL",
    "MODEL_CATALOG",
    "ModelInfo",
    "UnknownModelError",
    "get_model_info",
    "list_models",
    "resolve_model",
]

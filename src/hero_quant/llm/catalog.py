"""Built-in LLM model metadata and safe model-name resolution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Mapping


DEFAULT_MODEL = "gpt-4o-mini"

# 目录价没有版本链：effective_from 取「该模型首次公开可用日」，作为「不可能更早生效」
# 的保守下界，保证早于该日的时点查询一律不猜价。价格本身可能随后续调价变化，
# 因此 pricing 侧只标 source=llm.catalog，不承诺可回溯到历史真实账单。
# 新增条目必须显式填 effective_from；默认值 date.max 表示「生效日未知」，
# 时点查询恒视为未生效（fail-closed），禁止用今天的价冒充历史价。
_DEFAULT_EFFECTIVE_FROM = date.max

# 中文：已移除对常量集合的 no-op import-time guard（原检查恒为 False，仅 pass）；
# 真实 fail-fast 由下方 MODEL_CATALOG 校验保证。


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Catalog metadata; prices are USD per one million tokens."""

    name: str
    input_price_per_million_tokens: float
    output_price_per_million_tokens: float
    capabilities: tuple[str, ...]
    context_window: int | None = None
    effective_from: date = _DEFAULT_EFFECTIVE_FROM

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
            effective_from=date(2024, 7, 18),  # 公开可用日（价格生效下界）
        ),
        "gpt-4o": ModelInfo(
            name="gpt-4o",
            input_price_per_million_tokens=2.50,
            output_price_per_million_tokens=10.00,
            capabilities=("chat", "tool_calling", "structured_output"),
            context_window=128_000,
            effective_from=date(2024, 5, 13),  # 公开可用日（价格生效下界）
        ),
        "gpt-4.1-mini": ModelInfo(
            name="gpt-4.1-mini",
            input_price_per_million_tokens=0.40,
            output_price_per_million_tokens=1.60,
            capabilities=("chat", "tool_calling", "structured_output"),
            context_window=1_000_000,
            effective_from=date(2025, 4, 14),  # 公开可用日（价格生效下界）
        ),
        "gpt-4.1": ModelInfo(
            name="gpt-4.1",
            input_price_per_million_tokens=2.00,
            output_price_per_million_tokens=8.00,
            capabilities=("chat", "tool_calling", "structured_output"),
            context_window=1_000_000,
            effective_from=date(2025, 4, 14),  # 公开可用日（价格生效下界）
        ),
        "deepseek-chat": ModelInfo(
            name="deepseek-chat",
            input_price_per_million_tokens=0.27,
            output_price_per_million_tokens=1.10,
            capabilities=("chat", "tool_calling", "structured_output"),
            context_window=128_000,
            effective_from=date(2024, 5, 6),  # 公开可用日（价格生效下界）
        ),
        "deepseek-reasoner": ModelInfo(
            name="deepseek-reasoner",
            input_price_per_million_tokens=0.55,
            output_price_per_million_tokens=2.19,
            capabilities=("chat", "tool_calling", "reasoning"),
            context_window=128_000,
            effective_from=date(2025, 1, 20),  # 公开可用日（价格生效下界）
        ),
    }
)

class UnknownModelError(ValueError):
    """Raised when strict resolution receives a model absent from the catalog."""


if DEFAULT_MODEL not in MODEL_CATALOG:
    raise UnknownModelError(f"DEFAULT_MODEL {DEFAULT_MODEL!r} not in MODEL_CATALOG")


def _model_name(model: str | None) -> str:
    return str(model or "").strip()


def resolve_model(
    model: str | None = None,
    *,
    fallback: str = DEFAULT_MODEL,
    strict: bool = False,
) -> ModelInfo:
    """Resolve a model from the catalog, optionally failing instead of falling back."""
    import logging as _logging

    requested = _model_name(model)
    if requested in MODEL_CATALOG:
        return MODEL_CATALOG[requested]
    if strict:
        raise UnknownModelError(f"unknown LLM model: {requested or '<empty>'}")

    # non-strict fallback is fail-visible
    if requested:
        _logging.getLogger(__name__).warning("unknown LLM model %r, falling back to %r", requested, fallback)

    fallback_name = _model_name(fallback)
    if fallback_name in MODEL_CATALOG:
        return MODEL_CATALOG[fallback_name]
    raise UnknownModelError(f"unknown LLM fallback model: {fallback!r} (requested {requested!r})")


def get_model_info(model: str | None, *, fallback: str = DEFAULT_MODEL, strict: bool = False) -> ModelInfo:
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

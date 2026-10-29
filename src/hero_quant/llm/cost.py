"""LLM 用量与成本归集（W2 · PR-115）。

职责：把一次 LLM 调用归一化为 `LLMUsage`（provider/model/tokens/成本/延迟），
      提供单次记录（Prometheus + structlog）与按 run 汇总（`UsageLedger`）。

口径（与仓库既有「降级必须显式」契约一致）：
- 价格只取自 `llm/catalog.py::MODEL_CATALOG`（USD / 1M tokens），本模块不另建价目表；
- 目录外模型 `cost_usd=None`（不猜价），计入 `unpriced_models`，绝不返回 0 冒充免费；
- 后端没给 usage 时按字符启发式估算并置 `estimated=True`，不把估算值伪装成后端计量。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .catalog import MODEL_CATALOG, ModelInfo

logger = logging.getLogger(__name__)

try:  # 与 llm/client.py 同款：无 structlog 时退化为 stdlib logging
    import structlog as _structlog

    _slog: Any = _structlog.get_logger(__name__)
except Exception:  # pragma: no cover - 最小环境无 structlog
    _slog = None  # type: ignore[assignment]

PRICE_UNIT = 1_000_000
"""价目表单位：catalog 的价格是 USD / 1M tokens。"""

# Prometheus 收集器（惰性注册，重复注册/未安装时降级为 None，观测不得影响业务）
_METRICS_INIT = False
_TOKEN_COUNTER: Any = None
_COST_COUNTER: Any = None
_LATENCY_HIST: Any = None


def _init_metrics() -> None:
    """惰性注册成本指标；任何失败都降级为 None（best-effort）。"""
    global _METRICS_INIT, _TOKEN_COUNTER, _COST_COUNTER, _LATENCY_HIST
    if _METRICS_INIT:
        return
    _METRICS_INIT = True
    try:
        from prometheus_client import Counter, Histogram  # type: ignore
    except Exception:
        return
    try:
        _TOKEN_COUNTER = Counter(
            "hero_quant_llm_tokens_total",
            "LLM tokens by provider/model/kind",
            ["provider", "model", "kind"],
        )
    except Exception:
        _TOKEN_COUNTER = None
    try:
        _COST_COUNTER = Counter(
            "hero_quant_llm_cost_total",
            "LLM cost in USD by provider/model",
            ["provider", "model"],
        )
    except Exception:
        _COST_COUNTER = None
    try:
        _LATENCY_HIST = Histogram(
            "hero_quant_llm_latency_seconds",
            "LLM call latency in seconds by provider/model",
            ["provider", "model"],
            buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
        )
    except Exception:
        _LATENCY_HIST = None


def _pick_int(raw: Any, *names: str) -> int | None:
    """从 dict/对象里按候选名取整数；缺失或非法返回 None。"""
    for name in names:
        if isinstance(raw, Mapping):
            val = raw.get(name)
        else:
            val = getattr(raw, name, None)
        if val is None:
            continue
        try:
            return int(val)
        except (TypeError, ValueError):
            continue
    return None


def normalize_usage(raw: Any) -> tuple[int, int] | None:
    """归一化后端 usage 为 (input_tokens, output_tokens)。

    兼容 LangChain（input_tokens/output_tokens）、OpenAI（prompt_tokens/completion_tokens）
    与返回 dict 的适配器。拿不到任一侧时返回 None（交由调用方决定是否估算）。
    """
    if raw is None:
        return None
    inp = _pick_int(raw, "prompt_tokens", "input_tokens", "input")
    out = _pick_int(raw, "completion_tokens", "output_tokens", "output")
    if inp is None and out is None:
        return None
    return (max(0, inp or 0), max(0, out or 0))


def price_for(model: str | None) -> ModelInfo | None:
    """按模型名取价目；未知模型返回 None（不回落默认价，避免把贵模型算成便宜模型）。"""
    name = str(model or "").strip()
    if not name:
        return None
    info = MODEL_CATALOG.get(name)
    if info is not None:
        return info
    # 版本化后缀：gpt-4o-mini-2024-07-18 / deepseek-chat@v2
    for known in sorted(MODEL_CATALOG, key=len, reverse=True):
        if name.startswith(known + "-") or name.startswith(known + "@") or name.startswith(known + ":"):
            return MODEL_CATALOG[known]
    return None


def _cjk_count(text: str) -> int:
    """统计 CJK 字符数（中文约 1 字 ≈ 1 token）。"""
    count = 0
    for ch in text:
        code = ord(ch)
        if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF or 0x3000 <= code <= 0x303F:
            count += 1
    return count


def estimate_tokens(text: str | None) -> int:
    """字符启发式估算 token：CJK 1 字 ≈ 1 token，其余按 4 字符 ≈ 1 token。"""
    if not text:
        return 0
    body = str(text)
    cjk = _cjk_count(body)
    other = max(0, len(body) - cjk)
    return int(math.ceil(cjk + other / 4.0))


@dataclass(frozen=True)
class LLMUsage:
    """一次 LLM 调用的用量与成本快照。"""

    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    estimated: bool = False
    latency_s: float | None = None

    @property
    def total_tokens(self) -> int:
        return int(self.input_tokens) + int(self.output_tokens)

    @property
    def priced(self) -> bool:
        return self.cost_usd is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "input_tokens": int(self.input_tokens),
            "output_tokens": int(self.output_tokens),
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "priced": self.priced,
            "estimated": bool(self.estimated),
            "latency_s": self.latency_s,
        }


def build_usage(
    llm: Any,
    prompt: str,
    text: str | None = None,
    *,
    provider: str,
    model: str,
    latency_s: float | None = None,
    usage: Any = None,
    output_chars: int | None = None,
) -> LLMUsage:
    """构造 `LLMUsage`：优先后端 usage，缺失时按字符估算。

    `output_chars` 用于流式场景——此时没有完整文本，只累计了字符数。
    """
    raw = usage
    if raw is None and llm is not None:
        for attr in ("last_usage", "usage"):
            raw = getattr(llm, attr, None)
            if raw is not None:
                break
    normalized = normalize_usage(raw)
    estimated = False
    if normalized is None:
        estimated = True
        input_tokens = estimate_tokens(prompt)
        if output_chars is not None:
            output_tokens = int(math.ceil(max(0, output_chars) / 4.0))
        else:
            output_tokens = estimate_tokens(text)
    else:
        input_tokens, output_tokens = normalized

    info = price_for(model)
    cost: float | None = None
    if info is not None:
        cost = (input_tokens / PRICE_UNIT) * float(info.input_price_per_million_tokens) + (
            output_tokens / PRICE_UNIT
        ) * float(info.output_price_per_million_tokens)
        cost = round(cost, 8)

    return LLMUsage(
        provider=str(provider),
        model=str(model),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        estimated=estimated,
        latency_s=latency_s,
    )


def record_llm_usage(usage: LLMUsage) -> None:
    """记录单次用量：Prometheus 计数 + 结构化日志；任何失败都不得影响业务。"""
    _init_metrics()
    try:
        if _TOKEN_COUNTER is not None:
            _TOKEN_COUNTER.labels(provider=usage.provider, model=usage.model, kind="input").inc(usage.input_tokens)
            _TOKEN_COUNTER.labels(provider=usage.provider, model=usage.model, kind="output").inc(usage.output_tokens)
        if _COST_COUNTER is not None and usage.cost_usd is not None:
            _COST_COUNTER.labels(provider=usage.provider, model=usage.model).inc(float(usage.cost_usd))
        if _LATENCY_HIST is not None and usage.latency_s is not None:
            _LATENCY_HIST.labels(provider=usage.provider, model=usage.model).observe(float(usage.latency_s))
    except Exception as exc:  # pragma: no cover - 观测 best-effort
        logger.debug("record_llm_usage metrics failed error_code=metrics_emit_failed: %s", exc)
    try:
        if _slog is not None:
            _slog.info(
                "llm_usage",
                provider=usage.provider,
                model=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=usage.cost_usd,
                priced=usage.priced,
                estimated=usage.estimated,
                latency_ms=round(usage.latency_s * 1000, 3) if usage.latency_s is not None else None,
            )
    except Exception:  # pragma: no cover - 日志 best-effort
        pass


@dataclass
class UsageLedger:
    """按 run 汇总用量（评测/单次请求共用）。

    不做线程同步：调用方按 run 串行 add，与 AgentLoop 单线程语义一致。
    """

    items: list[LLMUsage] = field(default_factory=list)

    def add(self, usage: LLMUsage) -> None:
        self.items.append(usage)

    def merge(self, other: Iterable[LLMUsage]) -> None:
        self.items.extend(other)

    def summary(self) -> dict[str, Any]:
        """汇总 tokens/成本；未定价模型单列，不并进 total_cost 冒充完整成本。"""
        by_model: dict[str, dict[str, Any]] = {}
        by_provider: dict[str, dict[str, Any]] = {}
        priced_calls = 0
        unpriced_models: set[str] = set()
        total_cost = 0.0
        for usage in self.items:
            key = f"{usage.provider}/{usage.model}"
            bucket = by_model.setdefault(
                key,
                {
                    "provider": usage.provider,
                    "model": usage.model,
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0.0,
                    "priced": True,
                },
            )
            bucket["calls"] += 1
            bucket["input_tokens"] += usage.input_tokens
            bucket["output_tokens"] += usage.output_tokens
            prov = by_provider.setdefault(
                usage.provider, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
            )
            prov["calls"] += 1
            prov["input_tokens"] += usage.input_tokens
            prov["output_tokens"] += usage.output_tokens
            if usage.cost_usd is None:
                bucket["priced"] = False
                unpriced_models.add(usage.model)
            else:
                priced_calls += 1
                total_cost += float(usage.cost_usd)
                bucket["cost_usd"] += float(usage.cost_usd)
                prov["cost_usd"] += float(usage.cost_usd)

        for bucket in by_model.values():
            bucket["cost_usd"] = round(bucket["cost_usd"], 8)
        for prov in by_provider.values():
            prov["cost_usd"] = round(prov["cost_usd"], 8)

        calls = len(self.items)
        return {
            "calls": calls,
            "input_tokens": sum(u.input_tokens for u in self.items),
            "output_tokens": sum(u.output_tokens for u in self.items),
            "total_tokens": sum(u.total_tokens for u in self.items),
            "total_cost_usd": round(total_cost, 8) if priced_calls else None,
            "priced_calls": priced_calls,
            "unpriced_calls": calls - priced_calls,
            "cost_complete": calls > 0 and priced_calls == calls,
            "unpriced_models": sorted(unpriced_models),
            "by_model": by_model,
            "by_provider": by_provider,
        }

    def to_dict(self) -> dict[str, Any]:
        """trace/结果文件写入用的 dict 形态（同 summary）。"""
        return self.summary()


__all__ = [
    "LLMUsage",
    "PRICE_UNIT",
    "UsageLedger",
    "build_usage",
    "estimate_tokens",
    "normalize_usage",
    "price_for",
    "record_llm_usage",
]

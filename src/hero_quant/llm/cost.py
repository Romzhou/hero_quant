"""LLM 用量与成本归集（W2 · PR-115）。

职责：把一次 LLM 调用归一化为 `LLMUsage`（provider/model/tokens/成本/延迟），
      提供单次记录（Prometheus + structlog）与按 run 汇总（`UsageLedger`）。

口径（与仓库既有「降级必须显式」契约一致）：
- 价格只取自 `llm/catalog.py::MODEL_CATALOG`（USD / 1M tokens），本模块不另建价目表；
- 目录外模型 `cost_usd=None`（不猜价），计入 `unpriced_models`，绝不返回 0 冒充免费；
- 后端没给 usage 时按字符启发式估算并置 `estimated=True`，不把估算值伪装成后端计量；
- 免费源根本拿不到 usage 时走 `LLMUsage.from_free_source`：`usage_source="absent"`，
  token 保持 0，只记字符数/调用次数/耗时，**禁止**伪造 estimated token；
- `cost_usd` 字段保留给既有调用方；新的 `cost` 属性给出 `{"value", "status"}`，
  `status` 只有 `priced`/`unpriced` 两种，与 `cost_usd` 恒一致。
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

USAGE_SOURCES = ("provider", "estimated", "absent")
"""usage_source 取值：后端计量 / 字符启发式估算 / 免费源无计量。"""

ESTIMATED_METHOD_CHAR_HEURISTIC = "char_heuristic_cjk_4char"
"""字符启发式估算方法名（CJK 1 字 ≈ 1 token，其余 4 字符 ≈ 1 token）。"""


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
    """一次 LLM 调用的用量与成本快照。

    保留 input_tokens/output_tokens/cost_usd/estimated/latency_s 的原有顺序与语义，
    既有调用方（LLMFactory、gateway、evals、server）不需要改构造方式。
    """

    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    estimated: bool = False
    latency_s: float | None = None
    usage_source: str = ""
    estimated_method: str | None = None
    input_chars: int = 0
    output_chars: int = 0
    calls: int = 1
    duration_s: float | None = None

    def __post_init__(self) -> None:
        # 兼容旧构造：只给 estimated 不给 usage_source 时按 estimated 推导。
        source = str(self.usage_source or "").strip().lower()
        if not source:
            source = "estimated" if self.estimated else "provider"
        if source not in USAGE_SOURCES:
            raise ValueError(f"invalid usage_source {self.usage_source!r}; expected one of {USAGE_SOURCES}")
        object.__setattr__(self, "usage_source", source)
        # estimated 与 usage_source 互斥且恒一致：只看 usage_source 就够，避免两处口径打架。
        object.__setattr__(self, "estimated", source == "estimated")
        if source != "estimated":
            object.__setattr__(self, "estimated_method", None)
        if self.duration_s is None and self.latency_s is not None:
            object.__setattr__(self, "duration_s", self.latency_s)
        if int(self.calls) < 0:
            raise ValueError(f"calls must be >= 0, got {self.calls!r}")

    @classmethod
    def from_free_source(
        cls,
        provider: str,
        model: str,
        *,
        input_chars: int = 0,
        output_chars: int = 0,
        calls: int = 1,
        duration_s: float | None = None,
    ) -> "LLMUsage":
        """免费源（无 usage 接口）记录：只留字符数/次数/耗时，token 记 0 且不估算。

        没有 token 就没有可计价基数，cost 恒为 unpriced；禁止用 0 元或估算 token 冒充。
        """
        return cls(
            provider=str(provider),
            model=str(model),
            input_tokens=0,
            output_tokens=0,
            cost_usd=None,
            estimated=False,
            latency_s=duration_s,
            usage_source="absent",
            input_chars=max(0, int(input_chars)),
            output_chars=max(0, int(output_chars)),
            calls=max(0, int(calls)),
            duration_s=duration_s,
        )

    @property
    def total_tokens(self) -> int:
        return int(self.input_tokens) + int(self.output_tokens)

    @property
    def cost(self) -> dict[str, Any]:
        """定价状态：value 与 cost_usd 恒等，status 只有 priced/unpriced。"""
        if self.cost_usd is None:
            return {"value": None, "status": "unpriced"}
        return {"value": float(self.cost_usd), "status": "priced"}

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
            "cost": self.cost,
            "priced": self.priced,
            "estimated": bool(self.estimated),
            "usage_source": self.usage_source,
            "estimated_method": self.estimated_method,
            "input_chars": int(self.input_chars),
            "output_chars": int(self.output_chars),
            "calls": int(self.calls),
            "duration_s": self.duration_s,
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
    allow_estimate: bool = True,
) -> LLMUsage:
    """构造 `LLMUsage`：优先后端 usage，缺失时按字符估算。

    `output_chars` 用于流式场景——此时没有完整文本，只累计了字符数。

    估算路径只服务既有调用方（后端静默但确实跑了模型）；免费源调用方传
    `allow_estimate=False`：拿不到 usage 就转 `usage_source="absent"`，只记字符数/
    次数/耗时，绝不伪造 token。
    """
    raw = usage
    if raw is None and llm is not None:
        for attr in ("last_usage", "usage"):
            raw = getattr(llm, attr, None)
            if raw is not None:
                break
    normalized = normalize_usage(raw)
    if normalized is None and not allow_estimate:
        return LLMUsage.from_free_source(
            provider,
            model,
            input_chars=len(str(prompt or "")),
            output_chars=int(output_chars) if output_chars is not None else len(str(text or "")),
            calls=1,
            duration_s=latency_s,
        )
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

    chars_out = int(output_chars) if output_chars is not None else len(str(text or ""))
    return LLMUsage(
        provider=str(provider),
        model=str(model),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        estimated=estimated,
        latency_s=latency_s,
        usage_source="estimated" if estimated else "provider",
        estimated_method=ESTIMATED_METHOD_CHAR_HEURISTIC if estimated else None,
        input_chars=len(str(prompt or "")),
        output_chars=max(0, chars_out),
        calls=1,
        duration_s=latency_s,
    )


def record_llm_usage(usage: LLMUsage) -> None:
    """记录单次用量：走 `hero_quant.metrics` 统一注册表 + 结构化日志。

    中文：指标收集器只在 `metrics/__init__.py` 注册一处（t11 收口），本模块不再自建
    Counter，避免同名重复注册导致整组指标静默降级为 None。
    """
    try:
        from hero_quant.metrics import inc_llm_cost, inc_llm_tokens, observe_llm_latency

        inc_llm_tokens(usage.provider, usage.model, "input", usage.input_tokens)
        inc_llm_tokens(usage.provider, usage.model, "output", usage.output_tokens)
        if usage.cost_usd is not None:
            inc_llm_cost(usage.provider, usage.model, usage.cost_usd)
        if usage.latency_s is not None:
            observe_llm_latency(usage.provider, usage.model, usage.latency_s)
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
                cost_status=usage.cost["status"],
                priced=usage.priced,
                estimated=usage.estimated,
                usage_source=usage.usage_source,
                input_chars=usage.input_chars,
                output_chars=usage.output_chars,
                calls=usage.calls,
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
                    "input_chars": 0,
                    "output_chars": 0,
                    "duration_s": 0.0,
                },
            )
            bucket["calls"] += 1
            bucket["input_tokens"] += usage.input_tokens
            bucket["output_tokens"] += usage.output_tokens
            bucket["input_chars"] += int(usage.input_chars)
            bucket["output_chars"] += int(usage.output_chars)
            bucket["duration_s"] += float(usage.duration_s or 0.0)
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
            bucket["duration_s"] = round(bucket["duration_s"], 6)
        for prov in by_provider.values():
            prov["cost_usd"] = round(prov["cost_usd"], 8)

        calls = len(self.items)
        usage_sources: dict[str, int] = {}
        for usage in self.items:
            usage_sources[usage.usage_source] = usage_sources.get(usage.usage_source, 0) + 1
        durations = [float(u.duration_s) for u in self.items if u.duration_s is not None]
        return {
            "calls": calls,
            "llm_calls": sum(int(u.calls) for u in self.items),
            "input_tokens": sum(u.input_tokens for u in self.items),
            "output_tokens": sum(u.output_tokens for u in self.items),
            "total_tokens": sum(u.total_tokens for u in self.items),
            "input_chars": sum(int(u.input_chars) for u in self.items),
            "output_chars": sum(int(u.output_chars) for u in self.items),
            "duration_s": round(sum(durations), 6) if durations else None,
            "usage_sources": usage_sources,
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
    "ESTIMATED_METHOD_CHAR_HEURISTIC",
    "LLMUsage",
    "PRICE_UNIT",
    "USAGE_SOURCES",
    "UsageLedger",
    "build_usage",
    "estimate_tokens",
    "normalize_usage",
    "price_for",
    "record_llm_usage",
]

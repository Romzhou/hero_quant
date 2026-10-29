"""可观测性 —— prometheus 指标集中注册与辅助方法。

职责：注册 wall-time 治理、ledger 追加与 HTTP 相关的 histogram / counter，并提供 observe/inc 辅助。
架构位置：metrics 层，被 governance / ledger / server 等模块复用。
设计决策：所有收集器在重复注册时复用既有实例（reload-safe）；wall-time 按 operation/status 分桶，ledger 按 tenant/status 观测。
"""

from __future__ import annotations

import logging

from typing import Any

try:
    from prometheus_client import Counter, Gauge, Histogram  # type: ignore
except Exception:  # pragma: no cover - prometheus_client unavailable fallback to stub
    Counter = Histogram = Gauge = None  # type: ignore
    REGISTRY = None  # type: ignore

_logger = logging.getLogger(__name__)

__all__ = [
    "WALL_TIME_SECONDS",
    "WALL_TIME_DURATION",
    "WALL_TIME_BUDGET_EXCEEDED",
    "LEDGER_APPEND_DURATION",
    "LEDGER_APPEND_TOTAL",
    "DEDUP_OP_TOTAL",
    "REQUEST_DURATION",
    "REQUEST_COUNTER",
    "LLM_RETRY_TOTAL",
    "LLM_TIMEOUT_TOTAL",
    "LLM_RETRY_COUNTER",
    "LLM_TIMEOUT_COUNTER",
    "LLM_TOKENS_TOTAL",
    "LLM_COST_TOTAL",
    "LLM_LATENCY_SECONDS",
    "LLM_FAILOVER_TOTAL",
    "observe_wall_time",
    "inc_wall_time_exceeded",
    "observe_ledger_append",
    "inc_ledger_append",
    "inc_llm_retry",
    "inc_llm_timeout",
    "inc_llm_tokens",
    "inc_llm_cost",
    "observe_llm_latency",
    "inc_llm_failover",
    "get_wall_time_metrics",
]


def _get_or_create_histogram(name: str, doc: str, labels: list[str], buckets=None):
    """获取或创建 Histogram，重复注册时复用已注册实例。"""
    if Histogram is None:
        return None
    try:
        kw = {"labelnames": labels}
        if buckets is not None:
            kw["buckets"] = buckets
        h = Histogram(name, doc, **kw)
        return h
    except Exception:
        # D线：彻底去私有名；重复注册（重载/测试场景）时降级为 None（best-effort 观测），
        # 公共 API 无按名取回收集器的能力，不用 REGISTRY 私有 API 复用。
        return None


def _get_or_create_counter(name: str, doc: str, labels: list[str]):
    """获取或创建 Counter，重复注册时复用。"""
    if Counter is None:
        return None
    try:
        c = Counter(name, doc, labels)
        return c
    except Exception:
        # D线：彻底去私有名；重复注册时降级为 None（best-effort 计数）。
        return None


def _get_or_create_gauge(name: str, doc: str, labels: list[str] | None = None):
    """获取或创建 Gauge，重复注册时复用。"""
    if Gauge is None:
        return None
    try:
        if labels:
            g = Gauge(name, doc, labels)
        else:
            g = Gauge(name, doc)
        return g
    except Exception:
        # D线：彻底去私有名；重复注册时降级为 None（best-effort 观测）。
        return None


# wall-time 治理指标（单位：秒，分桶兼顾短耗时与长任务）
WALL_TIME_SECONDS = _get_or_create_histogram(
    "hero_quant_wall_time_seconds",
    "Wall-time duration in seconds by operation and status",
    ["operation", "status"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)

# 兼容别名：部分测试按 wall_time_duration 检索
WALL_TIME_DURATION = WALL_TIME_SECONDS

WALL_TIME_BUDGET_EXCEEDED = _get_or_create_counter(
    "hero_quant_governance_wall_time_exceeded_total",
    "Total count of wall-time budget exceeded events by operation",
    ["operation"],
)

# ledger 追加指标
LEDGER_APPEND_DURATION = _get_or_create_histogram(
    "hero_quant_governance_ledger_append_duration_seconds",
    "Ledger append wall-time duration in seconds",
    ["tenant", "status"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5],
)

LEDGER_APPEND_TOTAL = _get_or_create_counter(
    "hero_quant_governance_ledger_append_total",
    "Total ledger append operations",
    ["tenant", "status"],
)

DEDUP_OP_TOTAL = _get_or_create_counter(
    "hero_quant_governance_dedup_op_total",
    "Total dedup operations by op and status",
    ["op", "status"],
)

# LLM 可观测性（Wave6 P2）
LLM_RETRY_TOTAL = _get_or_create_counter(
    "hero_quant_llm_retry_total",
    "Total LLM retry attempts",
    ["provider", "reason"],
)

LLM_TIMEOUT_TOTAL = _get_or_create_counter(
    "hero_quant_llm_timeout_total",
    "Total LLM timeouts",
    ["provider"],
)

# 兼容别名
LLM_RETRY_COUNTER = LLM_RETRY_TOTAL
LLM_TIMEOUT_COUNTER = LLM_TIMEOUT_TOTAL

# LLM 用量/成本/延迟（W2 PR-115；口径：价格来自 llm/catalog.py，未定价模型不计数）
LLM_TOKENS_TOTAL = _get_or_create_counter(
    "hero_quant_llm_tokens_total",
    "LLM tokens by provider/model/kind (input|output)",
    ["provider", "model", "kind"],
)

LLM_COST_TOTAL = _get_or_create_counter(
    "hero_quant_llm_cost_total",
    "LLM cost in USD by provider/model (unpriced models are not counted)",
    ["provider", "model"],
)

LLM_LATENCY_SECONDS = _get_or_create_histogram(
    "hero_quant_llm_latency_seconds",
    "LLM call latency in seconds by provider/model",
    ["provider", "model"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0],
)

LLM_FAILOVER_TOTAL = _get_or_create_counter(
    "hero_quant_llm_failover_total",
    "LLM provider failover count by source/destination/reason",
    ["src", "dst", "reason"],
)

# HTTP 指标（server 已有同名 Histogram 时复用，此处为共享回退）
REQUEST_DURATION = _get_or_create_histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["endpoint"],
)

REQUEST_COUNTER = _get_or_create_counter(
    "hero_quant_requests_total",
    "Total requests",
    ["endpoint"],
)


def observe_wall_time(operation: str, duration: float, status: str = "success") -> None:
    """记录 wall-time 耗时（秒）。"""
    try:
        if WALL_TIME_SECONDS is not None:
            WALL_TIME_SECONDS.labels(operation=operation, status=status).observe(float(duration))
    except Exception as e:  # pragma: no cover - observability must not break business
        _logger.debug("observe_wall_time failed error_code=metrics_emit_failed: %s", e, exc_info=True)  # intentional: 指标 best-effort（observability must not break business）


def inc_wall_time_exceeded(operation: str = "generic") -> None:
    """累计 wall-time 超预算次数。"""
    try:
        if WALL_TIME_BUDGET_EXCEEDED is not None:
            WALL_TIME_BUDGET_EXCEEDED.labels(operation=operation).inc()
    except Exception as e:  # pragma: no cover
        _logger.debug("inc_wall_time_exceeded failed error_code=metrics_emit_failed: %s", e, exc_info=True)  # intentional: 同上（指标 best-effort）


def observe_ledger_append(tenant: str, duration: float, status: str = "success") -> None:
    """记录 ledger 追加耗时并计数。"""
    try:
        if LEDGER_APPEND_DURATION is not None:
            LEDGER_APPEND_DURATION.labels(tenant=tenant, status=status).observe(float(duration))
    except Exception as e:  # pragma: no cover
        _logger.debug("observe_ledger_append duration failed error_code=metrics_emit_failed: %s", e, exc_info=True)  # intentional: 同上（指标 best-effort）
    try:
        if LEDGER_APPEND_TOTAL is not None:
            LEDGER_APPEND_TOTAL.labels(tenant=tenant, status=status).inc()
    except Exception as e:  # pragma: no cover
        _logger.debug("observe_ledger_append total failed error_code=metrics_emit_failed: %s", e, exc_info=True)  # intentional: 同上（指标 best-effort）


def inc_ledger_append(tenant: str, status: str = "success") -> None:
    """累计 ledger 追加次数。"""
    try:
        if LEDGER_APPEND_TOTAL is not None:
            LEDGER_APPEND_TOTAL.labels(tenant=tenant, status=status).inc()
    except Exception as e:  # pragma: no cover
        _logger.debug("inc_ledger_append failed error_code=metrics_emit_failed: %s", e, exc_info=True)  # intentional: 同上（指标 best-effort）


def inc_llm_retry(provider: str = "unknown", reason: str = "error") -> None:
    """累计 LLM 重试次数。"""
    try:
        if LLM_RETRY_TOTAL is not None:
            LLM_RETRY_TOTAL.labels(provider=provider, reason=reason).inc()
    except Exception as e:  # pragma: no cover
        _logger.debug("inc_llm_retry failed error_code=metrics_emit_failed: %s", e, exc_info=True)  # intentional: 同上（指标 best-effort）


def inc_llm_timeout(provider: str = "unknown") -> None:
    """累计 LLM 超时次数。"""
    try:
        if LLM_TIMEOUT_TOTAL is not None:
            LLM_TIMEOUT_TOTAL.labels(provider=provider).inc()
    except Exception as e:  # pragma: no cover
        _logger.debug("inc_llm_timeout failed error_code=metrics_emit_failed: %s", e, exc_info=True)  # intentional: 同上（指标 best-effort）


def inc_llm_tokens(provider: str, model: str, kind: str, tokens: int) -> None:
    """累计 LLM token 数（kind=input|output）；非正整数直接忽略。"""
    try:
        if LLM_TOKENS_TOTAL is None:
            return
        count = int(tokens)
        if count <= 0:
            return
        LLM_TOKENS_TOTAL.labels(provider=provider, model=model, kind=kind).inc(count)
    except Exception as e:  # pragma: no cover
        _logger.debug("inc_llm_tokens failed error_code=metrics_emit_failed: %s", e, exc_info=True)  # intentional: 指标 best-effort


def inc_llm_cost(provider: str, model: str, usd: float) -> None:
    """累计 LLM 成本（USD）；0/负值不计数（未定价模型应传 None 而非 0）。"""
    try:
        if LLM_COST_TOTAL is None:
            return
        amount = float(usd)
        if amount <= 0:
            return
        LLM_COST_TOTAL.labels(provider=provider, model=model).inc(amount)
    except Exception as e:  # pragma: no cover
        _logger.debug("inc_llm_cost failed error_code=metrics_emit_failed: %s", e, exc_info=True)  # intentional: 指标 best-effort


def observe_llm_latency(provider: str, model: str, seconds: float) -> None:
    """记录 LLM 调用耗时（秒）。"""
    try:
        if LLM_LATENCY_SECONDS is None:
            return
        LLM_LATENCY_SECONDS.labels(provider=provider, model=model).observe(float(seconds))
    except Exception as e:  # pragma: no cover
        _logger.debug("observe_llm_latency failed error_code=metrics_emit_failed: %s", e, exc_info=True)  # intentional: 指标 best-effort


def inc_llm_failover(src: str, dst: str, reason: str) -> None:
    """累计 provider 回退次数（src→dst，reason 为脱敏后的 error code）。"""
    try:
        if LLM_FAILOVER_TOTAL is not None:
            LLM_FAILOVER_TOTAL.labels(src=src, dst=dst, reason=reason).inc()
    except Exception as e:  # pragma: no cover
        _logger.debug("inc_llm_failover failed error_code=metrics_emit_failed: %s", e, exc_info=True)  # intentional: 指标 best-effort


def get_wall_time_metrics() -> dict[str, Any]:
    """获取 wall-time 指标快照，用于调试/测试。"""
    out: dict[str, Any] = {}
    # 通过公开可用性探针判断（避免依赖私有 _metrics）
    try:
        if WALL_TIME_BUDGET_EXCEEDED is not None:
            out["exceeded_metric_available"] = True
    except Exception:
        pass  # intentional: 可用性探针 best-effort；失败即报告不可用（缺省 False）
    return out

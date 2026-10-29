"""成本归集契约：价目只来自 catalog、未知模型不猜价、估算值显式标记。"""

from __future__ import annotations

from hero_quant.llm.cost import (
    LLMUsage,
    UsageLedger,
    build_usage,
    estimate_tokens,
    normalize_usage,
    price_for,
    record_llm_usage,
)


def test_price_for_known_model_uses_catalog():
    info = price_for("gpt-4o-mini")

    assert info is not None
    assert info.input_price_per_million_tokens == 0.15
    assert info.output_price_per_million_tokens == 0.60


def test_price_for_versioned_suffix_matches_base_model():
    base = price_for("deepseek-chat")
    versioned = price_for("deepseek-chat-2026-01-01")

    assert versioned is not None and base is not None
    assert versioned.name == base.name


def test_price_for_unknown_model_is_none_not_default():
    assert price_for("acme-super-model") is None
    assert price_for("") is None
    assert price_for(None) is None


def test_normalize_usage_accepts_both_key_styles():
    assert normalize_usage({"prompt_tokens": 10, "completion_tokens": 4}) == (10, 4)
    assert normalize_usage({"input_tokens": 7, "output_tokens": 3}) == (7, 3)
    assert normalize_usage(None) is None
    assert normalize_usage({"total_tokens": 11}) is None


def test_normalize_usage_accepts_object_attributes():
    class Raw:
        prompt_tokens = 5
        completion_tokens = 6

    assert normalize_usage(Raw()) == (5, 6)


def test_estimate_tokens_counts_cjk_as_one():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("中文") == 2


def test_build_usage_computes_cost_from_backend_usage():
    llm = type("L", (), {"last_usage": {"prompt_tokens": 1000, "completion_tokens": 1000}})()

    usage = build_usage(llm, "prompt", "text", provider="openai", model="gpt-4o-mini")

    assert usage.estimated is False
    assert usage.input_tokens == 1000
    assert usage.output_tokens == 1000
    # 1000/1M*0.15 + 1000/1M*0.60
    assert usage.cost_usd == 0.00075
    assert usage.priced is True


def test_build_usage_marks_estimate_when_backend_silent():
    llm = type("L", (), {"last_usage": None, "usage": None})()

    usage = build_usage(llm, "中文提问", "中文回答", provider="openai", model="gpt-4o-mini")

    assert usage.estimated is True
    assert usage.input_tokens > 0
    assert usage.output_tokens > 0
    assert usage.cost_usd is not None


def test_build_usage_unknown_model_has_no_cost():
    usage = build_usage(None, "p", "t", provider="acme", model="acme-super-model")

    assert usage.cost_usd is None
    assert usage.priced is False


def test_usage_ledger_summary_distinguishes_unpriced():
    ledger = UsageLedger()
    ledger.add(LLMUsage("openai", "gpt-4o-mini", 1000, 500, cost_usd=0.001))
    ledger.add(LLMUsage("openai", "gpt-4o-mini", 2000, 1000, cost_usd=0.002))
    ledger.add(LLMUsage("acme", "acme-super-model", 100, 50, cost_usd=None))

    summary = ledger.summary()

    assert summary["calls"] == 3
    assert summary["input_tokens"] == 3100
    assert summary["output_tokens"] == 1550
    assert summary["total_cost_usd"] == 0.003
    assert summary["priced_calls"] == 2
    assert summary["unpriced_calls"] == 1
    assert summary["cost_complete"] is False
    assert summary["unpriced_models"] == ["acme-super-model"]
    assert summary["by_model"]["openai/gpt-4o-mini"]["calls"] == 2


def test_usage_ledger_empty_summary_has_no_fake_cost():
    summary = UsageLedger().summary()

    assert summary["calls"] == 0
    assert summary["total_cost_usd"] is None
    assert summary["cost_complete"] is False


def test_record_llm_usage_is_best_effort():
    usage = LLMUsage("openai", "gpt-4o-mini", 10, 5, cost_usd=0.0001, latency_s=0.2)
    # 未安装 prometheus / 重复注册都不允许抛错
    record_llm_usage(usage)

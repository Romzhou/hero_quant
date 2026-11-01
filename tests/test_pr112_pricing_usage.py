"""PR-112 用量与成本状态：priced/unpriced 与 usage_source 口径。"""

from __future__ import annotations

import pytest

from hero_quant.llm import cost as cost_mod
from hero_quant.llm.cost import LLMUsage, UsageLedger, build_usage


def test_build_usage_with_backend_usage_is_priced_provider_source():
    llm = type("L", (), {"last_usage": {"prompt_tokens": 1000, "completion_tokens": 500}})()

    usage = build_usage(llm, "prompt", "answer", provider="openai", model="gpt-4o-mini")

    assert usage.usage_source == "provider"
    assert usage.estimated is False
    assert usage.estimated_method is None
    assert usage.cost["status"] == "priced"
    assert usage.cost["value"] == usage.cost_usd, "cost 与 cost_usd 必须恒等"
    assert usage.input_chars == len("prompt") and usage.output_chars == len("answer")
    assert usage.calls == 1


def test_build_usage_estimate_path_marks_source_explicitly():
    llm = type("L", (), {"last_usage": None, "usage": None})()

    usage = build_usage(llm, "中文提问", "中文回答", provider="openai", model="gpt-4o-mini")

    assert usage.estimated is True
    assert usage.usage_source == "estimated"
    assert usage.estimated_method == cost_mod.ESTIMATED_METHOD_CHAR_HEURISTIC


def test_build_usage_free_source_mode_records_absent_not_estimate():
    llm = type("L", (), {"last_usage": None, "usage": None})()

    usage = build_usage(
        llm,
        "题目",
        "回答",
        provider="opencode-zen",
        model="big-pickle",
        allow_estimate=False,
        latency_s=0.5,
    )

    assert usage.usage_source == "absent"
    assert usage.estimated is False
    assert usage.input_tokens == 0 and usage.output_tokens == 0
    assert usage.input_chars == len("题目") and usage.output_chars == len("回答")
    assert usage.cost == {"value": None, "status": "unpriced"}
    assert usage.duration_s == 0.5


def test_free_source_records_chars_not_fake_tokens():
    usage = LLMUsage.from_free_source(
        "opencode-zen", "big-pickle", input_chars=120, output_chars=340, calls=2, duration_s=1.5
    )

    assert usage.usage_source == "absent"
    assert usage.estimated is False
    assert usage.input_tokens == 0 and usage.output_tokens == 0, "免费源禁止伪造 token"
    assert usage.input_chars == 120 and usage.output_chars == 340
    assert usage.calls == 2 and usage.duration_s == 1.5
    assert usage.cost == {"value": None, "status": "unpriced"}


def test_unpriced_model_is_none_not_zero():
    usage = build_usage(None, "p", "t", provider="acme", model="acme-super-model")

    assert usage.cost == {"value": None, "status": "unpriced"}
    assert usage.cost_usd is None
    assert usage.to_dict()["cost"]["value"] is None


def test_to_dict_cost_status_matches_cost_usd():
    priced = LLMUsage("openai", "gpt-4o-mini", 10, 5, cost_usd=0.001)
    unpriced = LLMUsage("acme", "acme-super-model", 10, 5, cost_usd=None)

    assert priced.to_dict()["cost"] == {"value": 0.001, "status": "priced"}
    assert unpriced.to_dict()["cost"] == {"value": None, "status": "unpriced"}
    for usage in (priced, unpriced):
        payload = usage.to_dict()
        assert payload["cost"]["value"] == payload["cost_usd"]
        assert payload["priced"] is (payload["cost"]["status"] == "priced")


def test_estimated_and_usage_source_are_mutually_exclusive():
    derived = LLMUsage("openai", "gpt-4o-mini", 1, 1, cost_usd=0.0, estimated=True)
    forced = LLMUsage("openai", "gpt-4o-mini", 1, 1, cost_usd=0.0, estimated=True, usage_source="provider")
    absent = LLMUsage("opencode-zen", "big-pickle", usage_source="absent")

    assert derived.usage_source == "estimated" and derived.estimated is True
    assert forced.usage_source == "provider" and forced.estimated is False
    assert absent.usage_source == "absent" and absent.estimated is False


def test_invalid_usage_source_is_rejected():
    with pytest.raises(ValueError, match="usage_source"):
        LLMUsage("openai", "gpt-4o-mini", usage_source="guessed")


def test_ledger_summary_keeps_free_source_call_and_duration():
    ledger = UsageLedger()
    ledger.add(LLMUsage.from_free_source("opencode-zen", "big-pickle", input_chars=10, output_chars=20, duration_s=0.4))

    summary = ledger.summary()

    assert summary["calls"] == 1 and summary["llm_calls"] == 1
    assert summary["total_tokens"] == 0
    assert summary["input_chars"] == 10 and summary["output_chars"] == 20
    assert summary["duration_s"] == 0.4
    assert summary["usage_sources"] == {"absent": 1}
    assert summary["total_cost_usd"] is None and summary["cost_complete"] is False

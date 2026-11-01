"""批次15：定价阀门接线（HERO_PRICING_BACKEND + allow_estimate=False）。

口径：
- catalog（默认）：仓内价目有价 → priced；目录外模型 → unpriced（None，不是 0）；
- null：一律 unpriced，即使模型在目录里；
- 免费源拿不到 usage：记 usage_source=absent + 字符数/耗时，**不伪造 estimated token**。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hero_quant.config.settings import Settings
from hero_quant.llm.cost import build_usage

_REPO = Path(__file__).resolve().parents[1]


def _usage(**kwargs):
    return build_usage(None, "hello world", "ok", provider="openai", model="gpt-4o-mini", **kwargs)


def test_default_backend_is_catalog_and_prices_known_model(monkeypatch):
    monkeypatch.delenv("HERO_PRICING_BACKEND", raising=False)
    assert Settings().pricing_backend == "catalog"
    usage = _usage(usage={"prompt_tokens": 1000, "completion_tokens": 500})
    assert usage.usage_source == "provider"
    assert usage.cost_usd == pytest.approx(1000 / 1e6 * 0.15 + 500 / 1e6 * 0.60)
    assert usage.cost["status"] == "priced"


def test_null_backend_makes_everything_unpriced(monkeypatch):
    monkeypatch.setenv("HERO_PRICING_BACKEND", "null")
    usage = _usage(usage={"prompt_tokens": 1000, "completion_tokens": 500})
    assert usage.cost_usd is None
    assert usage.cost["value"] is None and usage.cost["status"] == "unpriced"
    assert usage.total_tokens == 1500  # 计数照旧，只有钱是未知


def test_unknown_backend_is_fail_closed(monkeypatch):
    monkeypatch.setenv("HERO_PRICING_BACKEND", "bloomberg")
    with pytest.raises(ValueError, match="HERO_PRICING_BACKEND"):
        Settings()


def test_unknown_model_stays_unpriced_not_zero(monkeypatch):
    monkeypatch.delenv("HERO_PRICING_BACKEND", raising=False)
    usage = build_usage(None, "x", "y", provider="acme", model="acme-super-model",
                        usage={"prompt_tokens": 10, "completion_tokens": 5})
    assert usage.cost_usd is None and usage.cost["status"] == "unpriced"


def test_free_source_records_chars_not_fake_tokens(monkeypatch):
    monkeypatch.delenv("HERO_PRICING_BACKEND", raising=False)
    usage = build_usage(None, "1234567890", None, provider="local", model="gpt-4o-mini",
                        usage=None, output_chars=42, latency_s=1.5, allow_estimate=False)
    assert usage.usage_source == "absent"
    assert usage.estimated is False
    assert usage.input_tokens == 0 and usage.output_tokens == 0
    assert usage.input_chars == 10 and usage.output_chars == 42
    assert usage.calls == 1 and usage.duration_s == pytest.approx(1.5)
    assert usage.cost["status"] == "unpriced"


def test_gateway_and_server_pass_allow_estimate_false():
    gateway = (_REPO / "src/hero_quant/llm/gateway.py").read_text(encoding="utf-8")
    server = (_REPO / "src/hero_quant/api/server.py").read_text(encoding="utf-8")
    assert "allow_estimate=False" in gateway
    assert "allow_estimate=False" in server

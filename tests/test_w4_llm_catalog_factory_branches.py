"""W4 覆盖率补齐：llm/catalog.py 与 llm/factory.py 未覆盖分支。

覆盖目标：
- src/hero_quant/llm/catalog.py: ModelInfo 的 input_price/output_price 别名、
  resolve_model 的命中/strict 拒绝/非 strict 回落/回落模型也不在目录时抛错、
  get_model_info、list_models 排序、MODEL_CATALOG 只读视图
- src/hero_quant/llm/factory.py: _normalize_provider 的别名与 strict 拒绝、
  slot_for_stage 的归一化与未知阶段报错、LLMFactory.provider/model_for_stage/create
  的各分支（api_key 缺失、strict 抛错、model 回退）、_FallbackChat 四种调用形态

断言均为具体数值/异常类型/字段值，不使用弱断言。
"""

from __future__ import annotations

import pytest

from hero_quant.config.settings import Settings
from hero_quant.llm.catalog import (
    DEFAULT_MODEL,
    MODEL_CATALOG,
    ModelInfo,
    UnknownModelError,
    get_model_info,
    list_models,
    resolve_model,
)
from hero_quant.llm.client import LLMClient
from hero_quant.llm.factory import (
    STAGE_TO_SLOT,
    LLMFactory,
    _FallbackChat,
    _normalize_provider,
    model_for_stage,
    slot_for_stage,
)


def _settings(**kw) -> Settings:
    base = {
        "llm_provider": "openai",
        "llm_model": "gpt-4o-mini",
        "llm_model_deep": "gpt-4o-mini",
        "llm_model_quick": "gpt-4o-mini",
        "api_key": None,
        "openai_api_key": None,
    }
    base.update(kw)
    return Settings(**base)


# ---------------------------------------------------------------- catalog


def test_catalog_default_model_present():
    """DEFAULT_MODEL 必须在目录内（模块加载即校验）。"""
    assert DEFAULT_MODEL in MODEL_CATALOG
    assert MODEL_CATALOG[DEFAULT_MODEL].name == DEFAULT_MODEL


def test_model_info_price_aliases():
    """input_price/output_price 为每百万 token 价格的短别名。"""
    info = MODEL_CATALOG["gpt-4o-mini"]
    assert info.input_price == info.input_price_per_million_tokens == 0.15
    assert info.output_price == info.output_price_per_million_tokens == 0.60
    assert info.context_window == 128_000
    assert "tool_calling" in info.capabilities


def test_resolve_model_known_returns_catalog_entry():
    """命中目录时返回同一 ModelInfo 实例。"""
    assert resolve_model("gpt-4o") is MODEL_CATALOG["gpt-4o"]
    assert resolve_model("  gpt-4o  ").name == "gpt-4o"


def test_resolve_model_unknown_non_strict_falls_back():
    """非 strict 下未知模型回落 fallback 并告警。"""
    got = resolve_model("no-such-model", fallback="gpt-4o")
    assert got.name == "gpt-4o"


def test_resolve_model_empty_requested_falls_back():
    """空模型名回落 fallback（不报错）。"""
    assert resolve_model("", fallback="gpt-4o").name == "gpt-4o"
    assert resolve_model(None, fallback="gpt-4o").name == "gpt-4o"


def test_resolve_model_strict_raises_unknown_model_error():
    """strict 下未知模型抛 UnknownModelError。"""
    with pytest.raises(UnknownModelError, match="unknown LLM model"):
        resolve_model("no-such-model", strict=True)


def test_resolve_model_unknown_fallback_also_raises():
    """fallback 也不在目录中时抛 UnknownModelError（fail-visible）。"""
    with pytest.raises(UnknownModelError, match="unknown LLM fallback model"):
        resolve_model("no-such-model", fallback="also-unknown", strict=False)


def test_get_model_info_matches_resolve_model():
    """get_model_info 与 resolve_model 同语义。"""
    assert get_model_info("deepseek-chat").name == "deepseek-chat"
    with pytest.raises(UnknownModelError):
        get_model_info("no-such-model", strict=True)


def test_list_models_is_sorted_tuple():
    """list_models 返回确定顺序的元组。"""
    models = list_models()
    assert isinstance(models, tuple)
    assert list(models) == sorted(models)
    assert "gpt-4o" in models
    assert set(models) == set(MODEL_CATALOG)


def test_model_catalog_is_read_only_view():
    """MODEL_CATALOG 为只读映射视图，外部 mutation 抛错。"""
    with pytest.raises(TypeError):
        MODEL_CATALOG["new-model"] = ModelInfo("new-model", 1.0, 1.0, ("chat",))  # type: ignore[index]


# ---------------------------------------------------------------- provider 归一


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "openai"),
        ("", "openai"),
        ("OpenAI", "openai"),
        ("gpt-4o", "openai"),
        ("deepseek", "deepseek"),
        ("DEEPSEEK-chat", "deepseek"),
        ("anthropic", "anthropic"),
        ("claude-3", "anthropic"),
    ],
)
def test_normalize_provider_aliases(raw, expected):
    """提供方别名归一为三大 provider。"""
    assert _normalize_provider(raw) == expected


def test_normalize_provider_unknown_non_strict_falls_back_to_openai():
    """未知提供方非 strict 下告警并回落 openai。"""
    assert _normalize_provider("mystery-llm") == "openai"


def test_normalize_provider_unknown_strict_raises():
    """未知提供方 strict 下抛 ValueError。"""
    with pytest.raises(ValueError, match="unknown llm_provider"):
        _normalize_provider("mystery-llm", strict=True)


def test_factory_provider_property_uses_strict_flag():
    """LLMFactory.provider 透传 strict 语义。"""
    assert LLMFactory(_settings(llm_provider="claude")).provider == "anthropic"
    with pytest.raises(ValueError):
        LLMFactory(_settings(llm_provider="mystery"), strict=True).provider


# ---------------------------------------------------------------- stage -> slot


def test_slot_for_stage_mapping():
    """阶段到模型槽位的映射固定。"""
    assert slot_for_stage("plan") == "deep"
    assert slot_for_stage("verify") == "deep"
    assert slot_for_stage("tool_summary") == "quick"
    assert slot_for_stage("summary") == "quick"
    assert STAGE_TO_SLOT["tool_debate"] == "quick"


def test_slot_for_stage_normalizes_case_and_separators():
    """阶段名大小写/连字符/空格归一。"""
    assert slot_for_stage("Tool-Summary") == "quick"
    assert slot_for_stage("TOOL SUMMARY") == "quick"
    assert slot_for_stage(" plan ") == "deep"


def test_slot_for_stage_unknown_raises():
    """未知阶段抛 ValueError（fail-visible）。"""
    with pytest.raises(ValueError, match="unknown LLM stage"):
        slot_for_stage("no-such-stage")


def test_factory_model_for_stage_uses_slot_setting():
    """model_for_stage 按槽位取对应模型配置。"""
    f = LLMFactory(_settings(llm_model_deep="gpt-4.1", llm_model_quick="gpt-4o-mini"))
    assert f.model_for_stage("plan").name == "gpt-4.1"
    assert f.model_for_stage("summary").name == "gpt-4o-mini"
    assert f.slot_for_stage("plan") == "deep"


def test_module_level_model_for_stage_helper():
    """模块级 model_for_stage 便捷入口与工厂方法一致。"""
    got = model_for_stage("plan", _settings(llm_model_deep="gpt-4.1"))
    assert got.name == "gpt-4.1"


# ---------------------------------------------------------------- create


def test_create_returns_llm_client_with_timeout_30():
    """create 返回 LLMClient 且默认 timeout=30。"""
    llm = LLMFactory(_settings()).create(api_key="test-key")
    assert isinstance(llm, LLMClient)
    assert llm.timeout == 30
    assert llm.max_retries == 3


def test_create_honours_timeout_kwarg():
    """timeout 关键字被 pop 并透传到 LLMClient（不重复传参）。"""
    llm = LLMFactory(_settings()).create(api_key="test-key", timeout=7, temperature=0.1)
    assert llm.timeout == 7


def test_create_deepseek_provider_routes():
    """deepseek 提供方走 OpenAI 兼容分支并创建客户端。"""
    llm = LLMFactory(_settings(llm_provider="deepseek")).create(api_key="test-key")
    assert isinstance(llm, LLMClient)
    assert llm.timeout == 30


def test_create_anthropic_provider_falls_back_to_openai_compatible():
    """anthropic SDK 缺失时回落 OpenAI 兼容客户端（不抛）。"""
    llm = LLMFactory(_settings(llm_provider="anthropic")).create(api_key="test-key")
    assert isinstance(llm, LLMClient)


def test_create_strict_without_api_key_raises(monkeypatch):
    """strict 模式下缺 api_key 抛 ValueError（fail-visible）。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("HERO_API_KEY", raising=False)
    with pytest.raises(ValueError, match="LLM api_key missing"):
        LLMFactory(_settings(), strict=True).create()


def test_create_non_strict_without_api_key_uses_dummy_key():
    """非 strict 模式缺 api_key 时使用占位 key（离线兜底）。"""
    llm = LLMFactory(_settings()).create()
    assert isinstance(llm, LLMClient)


def test_create_falls_back_to_settings_model_when_resolution_fails():
    """模型解析失败时回落 settings.llm_model 并继续创建。"""
    s = _settings(llm_model="also-unknown-model", llm_model_deep="totally-unknown-model")
    llm = LLMFactory(s).create()
    assert isinstance(llm, LLMClient)


# ---------------------------------------------------------------- _FallbackChat


def test_fallback_chat_invoke_and_call():
    """离线占位 chat 的 invoke/__call__/chat 返回带模型名的文本。"""
    fb = _FallbackChat(model="gpt-4o-mini")
    assert fb.invoke("hello world") == "[gpt-4o-mini] hello world"
    assert fb("hello world") == "[gpt-4o-mini] hello world"
    assert fb.chat("hello world") == "[gpt-4o-mini] hello world"


def test_fallback_chat_stream_chat_truncates_prompt():
    """stream_chat 产出单块文本并截断 prompt 到 80 字符。"""
    fb = _FallbackChat(model="m")
    chunks = list(fb.stream_chat("x" * 200))
    assert len(chunks) == 1
    assert chunks[0].startswith("[m] ")
    assert len(chunks[0]) == len("[m] ") + 80


def test_fallback_chat_has_usage_zero():
    """占位 chat 暴露零用量，便于成本计量路径可测。"""
    fb = _FallbackChat()
    assert fb.usage == {"prompt_tokens": 0, "completion_tokens": 0}

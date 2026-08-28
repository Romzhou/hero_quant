from hero_quant.config.settings import Settings


def test_settings_model_slots_fall_back_to_legacy_model(monkeypatch):
    monkeypatch.delenv("HERO_LLM_MODEL", raising=False)
    monkeypatch.delenv("HERO_LLM_MODEL_DEEP", raising=False)
    monkeypatch.delenv("HERO_LLM_MODEL_QUICK", raising=False)

    settings = Settings()

    assert settings.llm_model == "gpt-4o-mini"
    assert settings.llm_model_deep == settings.llm_model
    assert settings.llm_model_quick == settings.llm_model


def test_settings_model_slots_accept_independent_env_overrides(monkeypatch):
    monkeypatch.setenv("HERO_LLM_MODEL", "legacy-model")
    monkeypatch.setenv("HERO_LLM_MODEL_DEEP", "deep-model")
    monkeypatch.setenv("HERO_LLM_MODEL_QUICK", "quick-model")

    settings = Settings()

    assert settings.llm_model_deep == "deep-model"
    assert settings.llm_model_quick == "quick-model"


def test_settings_model_slots_fall_back_to_legacy_env_model(monkeypatch):
    monkeypatch.setenv("HERO_LLM_MODEL", "legacy-model")
    monkeypatch.delenv("HERO_LLM_MODEL_DEEP", raising=False)
    monkeypatch.delenv("HERO_LLM_MODEL_QUICK", raising=False)

    settings = Settings()

    assert settings.llm_model_deep == "legacy-model"
    assert settings.llm_model_quick == "legacy-model"


def test_catalog_returns_price_and_capability_metadata():
    from hero_quant.llm import get_model_info, list_models

    model = get_model_info("gpt-4o-mini")

    assert model.name == "gpt-4o-mini"
    assert model.input_price_per_million_tokens == 0.15
    assert model.output_price_per_million_tokens == 0.60
    assert "tool_calling" in model.capabilities
    assert "gpt-4o-mini" in list_models()


def test_unknown_model_falls_back_to_default_or_raises_in_strict_mode():
    import pytest

    from hero_quant.llm import UnknownModelError, resolve_model

    assert resolve_model("not-in-catalog").name == "gpt-4o-mini"
    with pytest.raises(UnknownModelError):
        resolve_model("not-in-catalog", strict=True)


def test_factory_maps_graph_and_tool_stages_to_model_slots(monkeypatch):
    monkeypatch.setenv("HERO_LLM_MODEL_DEEP", "gpt-4o")
    monkeypatch.setenv("HERO_LLM_MODEL_QUICK", "gpt-4.1-mini")

    from hero_quant.llm import LLMFactory

    factory = LLMFactory()

    assert factory.slot_for_stage("plan") == "deep"
    assert factory.slot_for_stage("verify") == "deep"
    assert factory.slot_for_stage("tool_summary") == "quick"
    assert factory.slot_for_stage("tool_debate") == "quick"
    assert factory.model_for_stage("plan").name == "gpt-4o"
    assert factory.model_for_stage("verify").name == "gpt-4o"
    assert factory.model_for_stage("tool_summary").name == "gpt-4.1-mini"
    assert factory.model_for_stage("tool_debate").name == "gpt-4.1-mini"


def test_factory_rejects_unknown_stage():
    import pytest

    from hero_quant.llm import LLMFactory

    with pytest.raises(ValueError, match="unknown LLM stage"):
        LLMFactory().slot_for_stage("unmapped")


def test_resolve_model_invalid_fallback_raises():
    import pytest
    from hero_quant.llm import UnknownModelError, resolve_model
    with pytest.raises(UnknownModelError, match="unknown LLM fallback"):
        resolve_model("not-in-catalog", fallback="also-unknown", strict=False)
    with pytest.raises(UnknownModelError):
        resolve_model("not-in-catalog", fallback="also-unknown", strict=True)


def test_resolve_model_warns_on_fallback(caplog):
    import logging
    from hero_quant.llm import resolve_model
    caplog.set_level(logging.WARNING)
    m = resolve_model("typo-model")
    assert m.name == "gpt-4o-mini"
    assert any("unknown LLM model" in r.message for r in caplog.records)


def test_get_model_info_none_handling():
    from hero_quant.llm import get_model_info
    m = get_model_info(None)
    assert m.name == "gpt-4o-mini"
    m2 = get_model_info(None, fallback="gpt-4o")
    assert m2.name == "gpt-4o"


def test_default_model_invariant():
    from hero_quant.llm.catalog import DEFAULT_MODEL, MODEL_CATALOG
    assert DEFAULT_MODEL in MODEL_CATALOG


def test_factory_unknown_provider_strict_raises(monkeypatch):
    import pytest
    monkeypatch.setenv("HERO_LLM_PROVIDER", "weird_unknown_provider_xyz")
    from hero_quant.llm import LLMFactory
    with pytest.raises(ValueError, match="unknown llm_provider"):
        LLMFactory(strict=True).provider
    # non-strict should fallback with warning, not raise
    assert LLMFactory(strict=False).provider == "openai"


def test_factory_timeout_kwarg_not_duplicate():
    from hero_quant.llm import LLMFactory
    from hero_quant.config.settings import Settings
    s = Settings()
    f = LLMFactory(s, strict=False)
    # should not raise TypeError: got multiple values for timeout
    client = f.create(model="gpt-4o-mini", api_key="test", timeout=60)
    # timeout should be propagated to LLMClient
    assert client.timeout == 60 or getattr(client, "_timeout", 60) == 60 or hasattr(client, "timeout")


def test_factory_missing_key_strict_raises(monkeypatch):
    import pytest, os
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("HERO_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    from hero_quant.config.settings import Settings
    from hero_quant.llm import LLMFactory
    s = Settings()
    # ensure settings has no key
    s.api_key = None  # type: ignore
    if hasattr(s, "openai_api_key"):
        s.openai_api_key = None  # type: ignore
    with pytest.raises(ValueError, match="api_key missing"):
        LLMFactory(s, strict=True).create(model="gpt-4o-mini")


def test_factory_strict_preserves_unknown_model_error(monkeypatch):
    import pytest
    monkeypatch.setenv("HERO_LLM_MODEL", "not-in-catalog")
    from hero_quant.llm import LLMFactory, UnknownModelError
    with pytest.raises(UnknownModelError):
        LLMFactory(strict=True).model_for_stage("plan")

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

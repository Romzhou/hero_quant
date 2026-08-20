def test_settings_loads_env(monkeypatch):
    monkeypatch.setenv("HERO_LLM_PROVIDER", "deepseek")
    from hero_quant.config.settings import Settings

    s = Settings()
    assert s.llm_provider == "deepseek"
    assert s.llm_model is not None


def test_no_raw_getenv_outside_config():
    import pathlib

    allowed = pathlib.Path("src/hero_quant/config")
    for p in pathlib.Path("src").rglob("*.py"):
        if allowed in p.parents or p.parent == allowed:
            continue
        assert "os.getenv" not in p.read_text(encoding="utf-8"), f"raw getenv in {p}"

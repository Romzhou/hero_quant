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


def test_llm_base_url_config_field(monkeypatch):
    """HERO_LLM_BASE_URL → Settings.llm_base_url：空=不覆盖，值去尾斜杠（校验矩阵见 test_llm_base_url.py）。"""
    from hero_quant.config.settings import Settings

    monkeypatch.delenv("HERO_LLM_BASE_URL", raising=False)
    assert Settings().llm_base_url is None
    monkeypatch.setenv("HERO_LLM_BASE_URL", "http://localhost:3001/v1/")
    assert Settings().llm_base_url == "http://localhost:3001/v1"


def test_bench_config_narrow_exceptions_logged():
    """P2-7+8+9: config/date/sort narrow except already tested in test_bench; smoke import."""
    from hero_quant.backtest.bench import _effective_benchmark_map, _normalize_index
    # ensure functions exist and raise appropriately
    import pytest
    with pytest.raises((ValueError, TypeError)):
        _normalize_index(["bad-date-xyz"])

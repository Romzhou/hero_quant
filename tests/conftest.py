"""用例级隔离：每用例自动失效 data_mode 缓存与 Settings 缓存，避免 HERO_DATA_MODE 切换脏读。"""

import pytest


@pytest.fixture(autouse=True)
def _clear_data_mode_cache():
    """自动失效 registry 缓存与 Settings lru_cache，确保 synthetic/live 切换即时生效。"""
    # 预先清理，避免上游用例残留
    try:
        from hero_quant.data.registry import clear_settings_cache

        clear_settings_cache()
    except Exception:
        pass
    try:
        from hero_quant.config.settings import get_settings

        get_settings.cache_clear()  # type: ignore[attr-defined]
    except Exception:
        pass
    yield
    # 用例后清理，防止污染下游
    try:
        from hero_quant.data.registry import clear_settings_cache

        clear_settings_cache()
    except Exception as e:
        import logging

        logging.getLogger(__name__).warning("conftest clear_settings_cache failed: %s", e, exc_info=True)
    try:
        from hero_quant.config.settings import get_settings

        get_settings.cache_clear()  # type: ignore[attr-defined]
    except Exception as e:
        import logging

        logging.getLogger(__name__).warning("conftest get_settings cache_clear failed: %s", e, exc_info=True)

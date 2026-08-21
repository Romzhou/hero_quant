def test_pg_saver_memory_fallback():
    from hero_quant.checkpoint.postgres import get_saver

    s = get_saver("memory://test")
    assert s is not None

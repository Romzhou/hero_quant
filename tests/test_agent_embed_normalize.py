import math
import pathlib


def test_offline_l2_normalized():
    from hero_quant.agent.embed import _embed_offline

    v = _embed_offline("hello", 32)
    norm = math.sqrt(sum(x * x for x in v))
    assert abs(norm - 1.0) < 1e-6, f"L2 norm {norm} != 1.0"


def test_embed_cache_lock():
    src = pathlib.Path("src/hero_quant/agent/embed.py").read_text(encoding="utf-8")
    assert "_CACHE_LOCK" in src, "missing _CACHE_LOCK"
    assert "RLock" in src, "missing RLock"
    # provider 失效检查应被锁包裹
    assert "with _CACHE_LOCK" in src, "provider check not wrapped with _CACHE_LOCK"
    # 失效后应 clear + 记录 last_provider
    assert "cache_clear" in src
    assert "_last_provider" in src

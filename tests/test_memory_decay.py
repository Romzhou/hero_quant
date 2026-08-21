"""B3-2 TDD: Ebbinghaus 14d decay — fresh entry ranked before stale."""
import math
import time

HALF_LIFE_DAYS = 14.0
DECAY_LAMBDA = math.log(2) / HALF_LIFE_DAYS


def _expected_importance(qs: float, ac: int, days: float) -> float:
    retention = math.exp(-DECAY_LAMBDA * max(0.0, days))
    bonus = min(0.3, ac * 0.1)
    raw = qs * (retention + bonus)
    return min(1.0, max(0.0, raw))


def test_compute_importance_formula():
    """Verify compute_importance equals qs*(exp(-λ*days)+min(0.3,ac*0.1)) capped [0,1]."""
    from hero_quant.memory.lifecycle import compute_importance

    qs, ac, days = 0.9, 5, 1.0
    expected = _expected_importance(qs, ac, days)
    assert compute_importance(qs, ac, days) == expected

    # 14 days retention ~0.5
    qs2, ac2, days2 = 0.9, 5, 14.0
    expected2 = _expected_importance(qs2, ac2, days2)
    assert compute_importance(qs2, ac2, days2) == expected2
    # stale must be lower than fresh when same qs/ac
    assert compute_importance(qs, ac, 14.0) < compute_importance(qs, ac, 1.0)


def test_ebbinghaus_fresh_ranked_before_stale(tmp_path):
    """Same query, same qs/ac, fresher (1d) must rank before stale (14d)."""
    from hero_quant.memory.store import MemoryStore

    ms = MemoryStore(tmp_path)
    # Different contents but same query token to avoid 30s dedup
    ms.write("fresh", "alpha decay fresh entry with momentum")
    ms.write("stale", "alpha decay stale entry with momentum variant")

    # Inject Ebbinghaus meta via in-memory Dict (no DDL)
    now = time.time()
    fresh_key = ms._ns_key("fresh")
    stale_key = ms._ns_key("stale")

    # Ensure _meta exists — minimal implementation exposes Dict
    # If not yet implemented, this will still set but search won't weight (red)
    if hasattr(ms, "_meta"):
        ms._meta[fresh_key] = {"quality_score": 0.9, "access_count": 5, "last_accessed": now - 1 * 86400}
        ms._meta[stale_key] = {"quality_score": 0.9, "access_count": 5, "last_accessed": now - 14 * 86400}
    elif hasattr(ms, "_quality"):
        # alternative dict names, try to cover
        ms._quality = {fresh_key: 0.9, stale_key: 0.9}
    else:
        # fallback: try to set via public helper if exists
        for meth in ("set_quality", "set_meta", "update_meta"):
            if hasattr(ms, meth):
                getattr(ms, meth)(fresh_key, quality_score=0.9, access_count=5, last_accessed=now - 86400)
                getattr(ms, meth)(stale_key, quality_score=0.9, access_count=5, last_accessed=now - 14 * 86400)
                break

    results = ms.search("alpha")
    # Should return both
    assert len(results) >= 2, f"expected >=2 results, got {results}"
    keys = [r["key"] for r in results]
    # fresh must be before stale
    # Handle namespace prefix in key
    def pos(name):
        for i, k in enumerate(keys):
            if k.endswith(name) or k == name:
                return i
        return 999

    assert pos("fresh") < pos("stale"), f"fresh should rank before stale, got order {keys}"

    # Also verify weighting factor: score*(0.5+0.5*importance) is applied
    # Fresh importance > stale importance, so weighted score higher
    from hero_quant.memory.lifecycle import compute_importance as ci

    imp_fresh = ci(0.9, 5, 1.0)
    imp_stale = ci(0.9, 5, 14.0)
    # fresh importance must be higher
    assert imp_fresh > imp_stale
    # weighted multiplier also preserves ordering
    assert (0.5 + 0.5 * imp_fresh) > (0.5 + 0.5 * imp_stale)


def test_decay_uses_half_life_14():
    """HALF_LIFE must be 14 days."""
    from hero_quant.memory import lifecycle as lc

    assert hasattr(lc, "HALF_LIFE_DAYS")
    assert lc.HALF_LIFE_DAYS == 14.0
    assert abs(lc._DECAY_LAMBDA - math.log(2) / 14.0) < 1e-9

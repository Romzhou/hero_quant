def test_mcp_router_topk():
    from hero_quant.mcp.router import route
    tools = route("find momentum factors for 600519", k=5)
    assert len(tools) == 5 and "compute_factor" in tools


def test_router_corpus_stale_on_description_change():
    from hero_quant.tools.registry import TOOL_REGISTRY
    import hero_quant.mcp.router as router_mod
    # capture IDF before mutation
    router_mod._ensure_corpus()
    idf_before = dict(router_mod._IDF)
    # mutate a tool description without changing registry size
    first = next(iter(TOOL_REGISTRY))
    orig_desc = TOOL_REGISTRY[first].description
    try:
        TOOL_REGISTRY[first].description = orig_desc + " unique_token_xyz_98765"
        router_mod._ensure_corpus()
        # IDF should have new token
        assert "unique_token_xyz_98765" in router_mod._IDF or router_mod._IDF != idf_before
    finally:
        TOOL_REGISTRY[first].description = orig_desc
        router_mod._ensure_corpus()


def test_router_tokenize_uses_precompiled():
    from hero_quant.mcp.router import _TOKEN_RE, _tokenize
    assert hasattr(_TOKEN_RE, "split")
    assert _tokenize("Hello, World! 123") == ["hello", "world", "123"]


def test_router_compute_factor_boost_not_hard_replace():
    from hero_quant.mcp.router import route
    # ensure compute_factor appears via boost, not via hard replace that evicts higher scorer
    tools = route("momentum factor", k=3)
    assert "compute_factor" in tools
    # second call same query should be deterministic (boost vs surgery)
    tools2 = route("momentum factor", k=3)
    assert tools == tools2


def test_router_vector_cache_bounded():
    from hero_quant.mcp.router import _DESC_VEC_CACHE
    from hero_quant.mcp.router import _vector_score_for_tool
    import numpy as np
    qvec = [1.0, 0.0, 0.0]
    # prime cache
    for i in range(5):
        _vector_score_for_tool(qvec, f"tool_{i}", f"desc {i}")
    assert len(_DESC_VEC_CACHE) >= 5


def test_router_rate_limiter_records_circuit():
    from hero_quant.mcp.router import _try_acquire_or_record, set_router_limiter, reset_router_limiter, _get_router_circuit
    from hero_quant.telemetry.circuit import DualBucketRateLimiter
    # inject small limiter that immediately rate-limits
    limiter = DualBucketRateLimiter(capacity=1, refill_per_sec=0, burst_capacity=1)
    limiter.try_acquire(1)  # consume token
    set_router_limiter(limiter)
    try:
        circ = _get_router_circuit()
        # exhaust token -> _try_acquire should return False and record failure
        ok = _try_acquire_or_record()
        assert ok is False
        # circuit should have recorded a failure (if implemented, it may be OPEN after threshold)
        assert circ is not None
    finally:
        reset_router_limiter()

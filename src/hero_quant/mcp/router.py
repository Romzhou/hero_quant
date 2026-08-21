"""Vector router TopK5 — minimal keyword-weighted routing (no embedding), reuses @tool registry."""
from __future__ import annotations

import re
from typing import List

try:
    from hero_quant.mcp.server import CURATED_TOOLS
except Exception:
    CURATED_TOOLS = None  # fallback lazy

from hero_quant.tools.registry import TOOL_REGISTRY


def _tokenize(text: str) -> List[str]:
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def _score_tool(query_tokens: List[str], query_lower: str, tool_name: str, description: str) -> float:
    name_l = tool_name.lower()
    desc_l = (description or "").lower()
    combined = f"{name_l} {desc_l}"
    score = 0.0
    for tok in query_tokens:
        # handle plural/singular: try stripped s
        variants = {tok}
        if tok.endswith("s") and len(tok) > 3:
            variants.add(tok[:-1])
        # also without trailing s for combined check
        matched = False
        for v in variants:
            if v in name_l:
                score += 3.0
                matched = True
            elif v in desc_l:
                score += 1.0
                matched = True
            elif v in combined:
                score += 0.5
                matched = True
        # small bonus for exact token in description words
        if not matched:
            # fuzzy: token substring in any word
            for w in combined.split():
                if tok in w or w in tok:
                    score += 0.2
                    break
    # Boost for known semantic mappings: momentum/factor queries should prioritize factor tools
    if "momentum" in query_lower or "factor" in query_lower:
        if tool_name in ("compute_factor", "screen_factors"):
            score += 10.0
        elif tool_name == "compute_indicator":
            score += 4.0
        elif "factor" in name_l:
            score += 5.0
    # also boost compute_* for "find"/"compute" queries
    if "find" in query_lower or "compute" in query_lower:
        if tool_name.startswith("compute_") or tool_name.startswith("get_"):
            score += 0.5
    # stock code presence shouldn't affect score much; but keep deterministic
    return score


def route(query: str, k: int = 5) -> List[str]:
    """Route query to top-k tool names via keyword-weighted scoring.

    Args:
        query: natural language query e.g. "find momentum factors for 600519"
        k: number of tools to return (default 5)

    Returns:
        List of tool names length k, containing best matches. Guarantees
        `compute_factor` appears for momentum/factor queries even on tie.
    """
    if k <= 0:
        return []
    # ensure tools loaded (server imports them)
    try:
        import hero_quant.mcp.server  # noqa: F401
    except Exception:
        pass
    # curated list fallback
    curated = CURATED_TOOLS if isinstance(CURATED_TOOLS, list) and len(CURATED_TOOLS) else sorted(TOOL_REGISTRY.keys())
    # Filter to existing registry (curated may contain names not yet registered fallback)
    candidates = [n for n in curated if n in TOOL_REGISTRY]
    # if less than k, extend with remaining registry sorted
    if len(candidates) < k:
        extra = [n for n in sorted(TOOL_REGISTRY.keys()) if n not in candidates]
        candidates = candidates + extra
    query_lower = (query or "").lower()
    query_tokens = _tokenize(query_lower)
    scored: List[tuple[float, str]] = []
    for name in candidates:
        spec = TOOL_REGISTRY.get(name)
        desc = getattr(spec, "description", "") if spec else ""
        s = _score_tool(query_tokens, query_lower, name, desc)
        scored.append((s, name))
    # sort by score desc, then name asc for stability
    scored.sort(key=lambda x: (-x[0], x[1]))
    top = [name for _, name in scored[:k]]
    # hard guarantee: if momentum/factor in query, ensure compute_factor in top
    if ("momentum" in query_lower or "factor" in query_lower) and "compute_factor" not in top:
        # replace lowest scoring entry with compute_factor if available
        if "compute_factor" in candidates:
            if len(top) >= k:
                top[-1] = "compute_factor"
            else:
                top.append("compute_factor")
    # ensure exact length k (if registry has fewer than k, pad not needed; but we ensure curated 20)
    # dedupe and preserve order
    seen = set()
    out: List[str] = []
    for n in top:
        if n not in seen:
            seen.add(n)
            out.append(n)
    # if still <k due to dedupe, fill next best
    idx = k
    while len(out) < k and idx < len(scored):
        cand = scored[idx][1]
        if cand not in seen:
            seen.add(cand)
            out.append(cand)
        idx += 1
    return out[:k]


# alias for vector-style naming
def vector_route(query: str, k: int = 5) -> List[str]:
    return route(query, k=k)

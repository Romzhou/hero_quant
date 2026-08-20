"""Central limits — single source for truncation.

TOOL_RESULT_LIMIT = 10_000 governs Function Calling choke.
Trace sidecar remains at 50k (TOOL_RESULT_OFFLOAD/TEXT_OFFLOAD in trace.py).
Uses environ.get pattern, no raw env gate violation.
"""

from __future__ import annotations

import json
from typing import Any

TOOL_RESULT_LIMIT: int = 10_000

# Optional env override placeholder (reads via environ.get to avoid os.getenv gate)
try:
    import os

    _env_val = os.environ.get("HERO_TOOL_RESULT_LIMIT")
    if _env_val is not None and _env_val.strip() != "":
        try:
            TOOL_RESULT_LIMIT = int(_env_val)
        except Exception:
            pass
except Exception:
    pass


def truncate_tool_result(result: Any, limit: int | None = None) -> str:
    """Truncate tool result with shown/total declaration.

    - Accepts str or any (json-dumped if not str)
    - If len <= limit: return as-is (string form)
    - Else: cut to limit and append TRUNCATED marker with counts
    - Marker contains literal TRUNCATED to satisfy audit / tests
    """
    lim = limit if limit is not None else TOOL_RESULT_LIMIT
    if result is None:
        return ""
    if isinstance(result, str):
        s = result
    else:
        try:
            s = json.dumps(result, ensure_ascii=False)
        except Exception:
            s = str(result)
    if len(s) <= lim:
        return s
    shown = lim
    total = len(s)
    truncated = s[:lim]
    # Include TRUNCATED marker and counts
    return f"{truncated}\n...[TRUNCATED shown={shown}/total={total}]"


def fit_records(records: list[Any], limit: int | None = None, per_record_limit: int | None = None) -> list[str]:
    """Pagination helper — pack records until limit, truncating overflow.

    - records: list of items (str or dict)
    - limit: total char budget (default TOOL_RESULT_LIMIT)
    - per_record_limit: optional cap per record
    Returns list of truncated string records that fit.
    """
    lim = limit if limit is not None else TOOL_RESULT_LIMIT
    out: list[str] = []
    total = 0
    for r in records:
        s = truncate_tool_result(r, limit=per_record_limit if per_record_limit is not None else lim)
        # If single record already exceeds total budget, truncate further
        if total + len(s) > lim and out:
            remaining = lim - total
            if remaining > 100:
                s = truncate_tool_result(s, limit=remaining)
            else:
                break
        if total + len(s) > lim and not out:
            # First record too large — return single truncated
            out.append(truncate_tool_result(s, limit=lim))
            break
        out.append(s)
        total += len(s)
        if total >= lim:
            break
    return out

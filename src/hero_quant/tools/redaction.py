"""Tools redaction — FC choke + desensitization.

Wraps security.redaction (sink-aware) + config.limits truncation.
Big field overflow is truncated at TOOL_RESULT_LIMIT (10k) with TRUNCATED marker.
Trace sidecar (50k) is separate path in agent/trace.py.
"""

from __future__ import annotations

import json
from typing import Any

from hero_quant.config.limits import TOOL_RESULT_LIMIT, truncate_tool_result


def _maybe_redact(value: Any, sink: str = "result") -> Any:
    """Delegate to security redaction for dict/list payloads."""
    try:
        from hero_quant.security.redaction import redact_payload

        if isinstance(value, (dict, list)):
            return redact_payload(value, sink=sink)
        if isinstance(value, str):
            # Strings: also run sensitive pattern check via security helper
            from hero_quant.security.redaction import redact_payload as rp

            # Wrap string in dict to reuse pattern logic, then unwrap
            wrapped = {"_v": value}
            redacted = rp(wrapped, sink=sink)
            # If redacted wraps, check
            if redacted.get("_v") == "***":
                return "***"
            return redacted.get("_v", value)
    except Exception:
        pass
    return value


def redact_tool_result(result: Any, limit: int | None = None, sink: str = "result") -> str:
    """Redact + truncate tool result (FC formatting choke).

    - For str: redact secret patterns then truncate to limit (10k default)
    - For dict/list: sink-aware redaction then json dump + truncate
    - Returns str with TRUNCATED marker on overflow
    - Keeps len <= limit when truncated
    """
    lim = limit if limit is not None else TOOL_RESULT_LIMIT

    # Step 1: redaction (sink-aware)
    if isinstance(result, (dict, list)):
        redacted = _maybe_redact(result, sink=sink)
        # Convert to string for truncation budget
        try:
            s = json.dumps(redacted, ensure_ascii=False)
        except Exception:
            s = str(redacted)
    elif isinstance(result, str):
        # Redact string patterns
        r = _maybe_redact(result, sink=sink)
        s = r if isinstance(r, str) else str(r)
    else:
        # Other types -> json or str then redact
        try:
            s = json.dumps(result, ensure_ascii=False)
        except Exception:
            s = str(result)
        s = _maybe_redact(s, sink=sink) if isinstance(s, str) else s
        if not isinstance(s, str):
            s = str(s)

    # Step 2: overflow truncation via limits helper (adds TRUNCATED marker)
    if len(s) <= lim:
        return s
    return truncate_tool_result(s, limit=lim)

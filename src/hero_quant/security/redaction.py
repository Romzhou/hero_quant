"""Redaction waterfall — ARGUMENTS_SINK most strict, RESULT_SINK lenient.

ARGUMENTS_SINK: redacts all sensitive keys + secret patterns.
RESULT_SINK: allows content field to pass through, still redacts top-level secrets.
Patterns: Bearer token, sk-..., AKIA..., JWT (eyJ...)
"""

from __future__ import annotations

import re
from typing import Any

ARGUMENTS_SINK = "arguments"
RESULT_SINK = "result"

# Sensitive key exact match (lowercased)
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "api-key",
    "secret",
    "password",
    "passwd",
    "token",
    "access_token",
    "refresh_token",
    "client_secret",
    "private_key",
}

# Substring match for keys containing these
_SENSITIVE_SUBSTRINGS = ("api_key", "apikey", "secret", "password", "token")

# Secret value patterns — waterfall strictness differs by sink
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-_\.=~\+/]+=*", re.IGNORECASE)
_SK_RE = re.compile(r"sk-[A-Za-z0-9]{10,}")
_AKIA_RE = re.compile(r"AKIA[0-9A-Z]{16}")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
# Generic long hex/base64 token
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{32,}")

_REDACTED = "***"


# llm_usage VCR keys should not be redacted (C1-2)
_ALLOW_TOKENS = {"input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "prompttokens", "completiontokens", "generated_tokens"}


def _is_sensitive_key(key: str) -> bool:
    lk = key.lower()
    if lk in _ALLOW_TOKENS:
        return False
    if lk in _SENSITIVE_KEYS:
        return True
    for sub in _SENSITIVE_SUBSTRINGS:
        if sub in lk:
            # but allow llm_usage token keys (exact allowlist handled above, keep substring allow)
            # e.g., "input_tokens" already returned False, so remaining token matches are true secrets like "access_token"
            return True
    return False


def _redact_string(value: str, sink: str) -> str:
    # ARGUMENTS_SINK is strictest — redact any secret pattern
    if sink == ARGUMENTS_SINK:
        if _BEARER_RE.search(value):
            return _REDACTED
        if _SK_RE.search(value):
            return _REDACTED
        if _AKIA_RE.search(value):
            return _REDACTED
        if _JWT_RE.search(value):
            return _REDACTED
        # For arguments sink, also treat any value for sensitive-looking string that looks like token
        # but keep simple: redact sk- prefix already handled
        return value
    # RESULT_SINK: still redact top-level secrets but allow content field later
    if sink == RESULT_SINK:
        if _BEARER_RE.search(value) or _SK_RE.search(value) or _AKIA_RE.search(value) or _JWT_RE.search(value):
            return _REDACTED
        return value
    return value


def redact_payload(payload: Any, sink: str = ARGUMENTS_SINK) -> Any:
    """Redact payload per sink waterfall.

    - payload dict: iterate keys, redact sensitive keys to "***" regardless of value.
      For other keys, redact value if secret pattern matches (sink-aware).
      Recurses into nested dicts/lists.
    - RESULT_SINK allows 'content' key to pass through unredacted (for tool results).
    - ARGUMENTS_SINK redacts everything including content-adjacent secrets.
    """
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for k, v in payload.items():
            # RESULT_SINK lenient: allow 'content' to pass through
            if sink == RESULT_SINK and k == "content":
                out[k] = v
                continue
            if _is_sensitive_key(k):
                out[k] = _REDACTED
                continue
            if isinstance(v, dict):
                out[k] = redact_payload(v, sink=sink)
            elif isinstance(v, list):
                out[k] = [redact_payload(item, sink=sink) if isinstance(item, (dict, list, str)) else item for item in v]
            elif isinstance(v, str):
                out[k] = _redact_string(v, sink=sink)
            else:
                out[k] = v
        return out
    elif isinstance(payload, list):
        return [redact_payload(item, sink=sink) for item in payload]
    elif isinstance(payload, str):
        return _redact_string(payload, sink=sink)
    else:
        return payload


# Alias for sink-aware redaction of tool results
def redact_tool_result(result: Any, sink: str = RESULT_SINK) -> Any:
    return redact_payload(result, sink=sink)

"""Minimal security helpers - HMAC + Host whitelist + Bearer/sk redaction.

- check_host(host, whitelist): if whitelist empty/None -> allow all locally;
  else check Host header against whitelist CSV from env HERO_HOST_WHITELIST.
- verify_hmac: dual-mode — (payload, signature, secret) HMAC-SHA256 AND
  verify_hmac(request) placeholder that checks Authorization Bearer/sk prefix
  via redaction patterns (Bearer/sk-/AKIA/JWT).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
from typing import Any

# Re-use redaction patterns to detect secret prefixes without leaking

_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-_\.=~\+/]+=*", re.IGNORECASE)
_SK_RE = re.compile(r"sk-[A-Za-z0-9]{10,}")
_AKIA_RE = re.compile(r"AKIA[0-9A-Z]{16}")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")


def _get_whitelist_from_env() -> list[str]:
    raw = os.environ.get("HERO_HOST_WHITELIST", "")
    if not raw or not raw.strip():
        return []
    # CSV split
    parts = [h.strip() for h in raw.split(",")]
    return [p for p in parts if p]


def _normalize_host(host: str) -> str:
    if not host:
        return ""
    # strip port, lower-case, strip whitespace
    return host.split(":")[0].strip().lower()


def check_host(host: str, allowed_hosts: list[str] | None = None) -> bool:
    """Check host against whitelist.

    - If allowed_hosts is None, loads from env HERO_HOST_WHITELIST CSV.
    - Empty whitelist => allow all locally (offline/test friendly).
    - Otherwise exact host (port-stripped, case-insensitive) must be in whitelist.
    """
    if allowed_hosts is None:
        allowed_hosts = _get_whitelist_from_env()
    if not allowed_hosts:
        return True
    host_norm = _normalize_host(host)
    allowed_norm = [_normalize_host(h) for h in allowed_hosts]
    return host_norm in allowed_norm


def verify_hmac(payload: bytes | Any, signature: str | None = None, secret: str | None = None) -> bool:
    """Verify HMAC-SHA256 signature.

    Dual-mode placeholder:
    - Classic: verify_hmac(payload_bytes, signature_hex, secret) -> HMAC compare.
    - Request: verify_hmac(request) where request has .headers with Authorization
      containing Bearer/sk-/AKIA/JWT prefix (checked via redaction regex). In that
      mode, signature/secret may be omitted and we just validate header presence.
    """
    # Request placeholder mode: first arg looks like FastAPI Request
    if hasattr(payload, "headers") or hasattr(payload, "scope"):
        # treat payload as request
        request = payload
        # Extract headers dict-like
        headers = {}
        try:
            # FastAPI Starlette Request.headers is case-insensitive
            h = getattr(request, "headers", {})
            # headers may be Headers object — convert via get
            if hasattr(h, "get"):
                auth = h.get("Authorization") or h.get("authorization") or ""
                # Also check X-API-Key style
                api_key = h.get("X-API-Key") or h.get("x-api-key") or ""
                combined = f"{auth} {api_key}".strip()
            else:
                combined = ""
        except Exception:
            combined = ""
        if not combined:
            # Try dict access fallback
            try:
                combined = str(headers)
            except Exception:
                combined = ""
        # Check via redaction patterns: Bearer, sk-, AKIA, JWT
        if _BEARER_RE.search(combined):
            return True
        if _SK_RE.search(combined):
            return True
        if _AKIA_RE.search(combined):
            return True
        if _JWT_RE.search(combined):
            return True
        # Placeholder: if no secret pattern, consider missing auth as fail
        # For offline/local where no auth required, we treat empty as True? But
        # spec says verify_hmac(request) checks Bearer/sk prefix via redaction,
        # so we return False when no recognizable prefix.
        # Keep lenient: if combined empty, return True for local (no whitelist)?
        # To avoid breaking local, return True when no header but whitelist empty?
        # We choose: empty => False (needs auth), but check_host already gates.
        return False

    # Classic HMAC bytes mode
    if not isinstance(payload, (bytes, bytearray)):
        # allow str payload for convenience
        if isinstance(payload, str):
            payload = payload.encode()
        else:
            return False
    if signature is None or secret is None:
        return False
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_request_auth(request: Any) -> bool:
    """Explicit request auth verify via redaction patterns (alias)."""
    return verify_hmac(request, None, None)


def is_host_allowed(request: Any, allowed_hosts: list[str] | None = None) -> bool:
    """Helper to check Host header from FastAPI Request against whitelist."""
    host = ""
    try:
        # Try request.headers.get("host")
        h = getattr(request, "headers", {})
        if hasattr(h, "get"):
            host = h.get("host") or h.get("Host") or ""
        if not host and hasattr(request, "url"):
            # Fallback to URL hostname
            host = getattr(request.url, "hostname", "") or ""
        # Also try request.client.host as last resort
        if not host and hasattr(request, "client"):
            host = getattr(request.client, "host", "") or ""
    except Exception:
        host = ""
    return check_host(host, allowed_hosts)


# Backwards compat alias for HMAC X-API-Key style
def verify_api_key(request: Any, expected_key: str | None = None) -> bool:
    """Check X-API-Key header against expected (or HERO_API_KEY env)."""
    if expected_key is None:
        expected_key = os.environ.get("HERO_API_KEY", "")
        if not expected_key:
            # No key configured locally => allow
            return True
    try:
        h = getattr(request, "headers", {})
        provided = h.get("X-API-Key") or h.get("x-api-key") or ""
    except Exception:
        provided = ""
    if not provided:
        return False
    return hmac.compare_digest(provided.strip(), expected_key.strip())

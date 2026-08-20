"""Minimal security helpers - HMAC + Host whitelist placeholder."""

import hmac
import hashlib


def verify_hmac(payload: bytes, signature: str, secret: str) -> bool:
    """Verify HMAC-SHA256 signature."""
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def check_host(host: str, allowed_hosts: list[str] | None = None) -> bool:
    """Check host against whitelist. Empty whitelist allows all (placeholder)."""
    if not allowed_hosts:
        return True
    return host in allowed_hosts

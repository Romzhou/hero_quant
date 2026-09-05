"""FAIL-first repro tests for Lane E3 (retest271): security + llm + otel + tools.

DO NOT edit existing test files — all repro tests live here.
Each test must FAIL before the fix and PASS after.
"""
from __future__ import annotations

import hashlib
import hmac


# ============================================================================
# api/security.py — 4 items
# ============================================================================

def test_e3_security_classic_hmac_empty_secret_fail_closed():
    """[security·high] Classic HMAC path must fail closed on empty secret/signature."""
    from hero_quant.api.security import verify_hmac

    payload = b"hello world"
    # empty secret: attacker forging with unset key must NOT verify
    forged = hmac.new(b"", payload, hashlib.sha256).hexdigest()
    assert verify_hmac(payload, forged, "") is False
    # empty signature must also fail closed
    assert verify_hmac(payload, "", "s3cr3t") is False
    # sane path still works
    sig = hmac.new(b"s3cr3t", payload, hashlib.sha256).hexdigest()
    assert verify_hmac(payload, sig, "s3cr3t") is True
    assert verify_hmac(payload, None, "s3cr3t") is False
    assert verify_hmac(payload, sig, None) is False


def test_e3_security_redis_error_falls_back_to_memory():
    """[bug·high] redis.exceptions.RedisError must be caught → memory fallback."""
    import hero_quant.api.security as sec_mod

    try:
        from redis.exceptions import ConnectionError as RedisConnectionError
    except ImportError:
        RedisConnectionError = None
    assert RedisConnectionError is not None, "redis-py required for this test"

    class BoomRedis:
        def set(self, *a, **k):
            raise RedisConnectionError("redis down")

        def getdel(self, *a, **k):
            raise RedisConnectionError("redis down")

        def eval(self, *a, **k):
            raise RedisConnectionError("redis down")

    orig = sec_mod._get_redis_for_ticket
    sec_mod._get_redis_for_ticket = lambda: BoomRedis()  # noqa: E731
    try:
        t = sec_mod.issue_ticket(ttl=60)
        assert isinstance(t, str) and t
        assert sec_mod.consume_ticket(t) is True
        assert sec_mod.consume_ticket(t) is False  # single-use
    finally:
        sec_mod._get_redis_for_ticket = orig


def test_e3_security_sync_body_explicit_empty_not_conflated():
    """[bug·medium] Explicit empty string body must NOT fall back to _body cache.

    `signature=""` falls into the request.body() else-branch (falsy str),
    then `if body == b"":` unconditionally falls back to `_body`/`_content`,
    verifying an explicit empty body against the wrong (cached) bytes.
    """
    import os

    from hero_quant.api.security import verify_hmac

    secret = "s3cr3t-e3"
    os.environ["HERO_HMAC_SECRET"] = secret
    try:
        sig_empty = hmac.new(secret.encode(), b"", hashlib.sha256).hexdigest()
        sig_cached = hmac.new(secret.encode(), b"something-else-entirely", hashlib.sha256).hexdigest()

        def _mkreq(sig):
            class CachedReq:
                headers = {"X-HMAC-Signature": sig}
                _body = b"something-else-entirely"

                def body(self):
                    async def _coro():
                        return b"something-else-entirely"

                    return _coro()

            return CachedReq()

        # explicit empty string body + sig over b"" → must verify True
        assert verify_hmac(_mkreq(sig_empty), "", secret) is True
        # no explicit body → back-compat _body fallback still works
        assert verify_hmac(_mkreq(sig_cached), None, secret) is True
    finally:
        os.environ.pop("HERO_HMAC_SECRET", None)


def test_e3_security_collision_retry_checks_second_set():
    """[bug·low] Second SET NX falsy → must NOT return un-stored ticket."""
    import hero_quant.api.security as sec_mod

    class AlwaysCollide:
        def set(self, *a, **k):
            return None  # NX collision every time

        def getdel(self, *a, **k):
            return None

    orig = sec_mod._get_redis_for_ticket
    sec_mod._get_redis_for_ticket = lambda: AlwaysCollide()  # noqa: E731
    try:
        t = sec_mod.issue_ticket(ttl=60)
        # ticket must actually be consumable (stored somewhere), not a ghost
        assert sec_mod.consume_ticket(t) is True
    finally:
        sec_mod._get_redis_for_ticket = orig

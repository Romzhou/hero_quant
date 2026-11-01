"""PR-151 接线：PII 加密口令经 secrets provider（vault/kms 时不回落 env）。

口径：口令拿不到 = 拒绝加解密（RuntimeError），**绝不**降级成明文；
env 后端下仍可回退 `Settings.pii_encryption_key`（同源 env）。
"""

from __future__ import annotations

import logging

import pytest

pytest.importorskip("cryptography")

from cryptography.fernet import Fernet  # noqa: E402

from hero_quant.api.middleware import pii as pii_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_pii_state(monkeypatch):
    """模块级缓存/告警标志逐用例复位（否则第一个用例的 key 会毒化后续）。"""
    monkeypatch.delenv("PII_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("HERO_SECRETS_BACKEND", raising=False)
    monkeypatch.setattr(pii_mod, "_PII_KEY", None, raising=True)
    monkeypatch.setattr(pii_mod, "_WARNED_NO_KEY", False, raising=True)
    monkeypatch.setattr(pii_mod, "_LAST_REASON", None, raising=True)
    yield


def _set_valid_key(monkeypatch) -> str:
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("PII_ENCRYPTION_KEY", key)
    return key


def test_env_backend_roundtrip(monkeypatch):
    _set_valid_key(monkeypatch)
    token = pii_mod.pii_encrypt("13800000000")
    assert token != "13800000000"
    assert pii_mod.pii_decrypt(token) == "13800000000"
    assert pii_mod.pii_key_status() == {"configured": True, "reason": None, "crypto_available": True}


@pytest.mark.parametrize("backend", ["vault", "kms"])
def test_unimplemented_backend_refuses_instead_of_plaintext(monkeypatch, backend):
    _set_valid_key(monkeypatch)
    monkeypatch.setenv("HERO_SECRETS_BACKEND", backend)
    status = pii_mod.pii_key_status()
    assert status["configured"] is False
    assert status["reason"] == "secrets_backend_unavailable"
    with pytest.raises(RuntimeError, match="拒绝明文"):
        pii_mod.pii_encrypt("13800000000")
    with pytest.raises(RuntimeError, match="PII"):
        pii_mod.pii_decrypt("gAAAAA-not-a-real-token")


def test_missing_key_reports_pii_key_missing(monkeypatch):
    monkeypatch.setenv("HERO_SECRETS_BACKEND", "env")
    assert pii_mod.pii_key_status()["reason"] == "pii_key_missing"
    with pytest.raises(RuntimeError):
        pii_mod.pii_encrypt("13800000000")


def test_invalid_key_is_reported_not_cached(monkeypatch):
    monkeypatch.setenv("PII_ENCRYPTION_KEY", "not-a-fernet-key")
    status = pii_mod.pii_key_status()
    assert status == {"configured": False, "reason": "pii_key_invalid", "crypto_available": True}
    assert pii_mod._PII_KEY is None  # noqa: SLF001 — 无效 key 不得入缓存


def test_legacy_noenc_still_rejected_by_default(monkeypatch):
    _set_valid_key(monkeypatch)
    with pytest.raises(RuntimeError, match="!NOENC!"):
        pii_mod.pii_decrypt("!NOENC!13800000000")


def test_key_value_never_logged(monkeypatch, caplog):
    key = _set_valid_key(monkeypatch)
    monkeypatch.setenv("HERO_SECRETS_BACKEND", "vault")
    with caplog.at_level(logging.DEBUG):
        pii_mod.pii_key_status()
        with pytest.raises(RuntimeError):
            pii_mod.pii_encrypt("13800000000")
    assert key not in caplog.text
    assert "secrets_backend_unavailable" in caplog.text

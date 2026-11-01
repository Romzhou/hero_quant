"""PR-151 接线：PG 口令经 secrets provider 注入 DSN（vault/kms 时不回落 env）。

规则：只给「有用户名、没口令」的 PG DSN 注入；显式口令优先；无 userinfo 不动；
provider 取不到口令时保持原样（连接失败按既有语义暴露），绝不偷偷用明文。
"""

from __future__ import annotations

import logging
from urllib.parse import unquote

import pytest

from hero_quant.config import settings as s

_DSN_ENVS = ("HERO_CHECKPOINT_DSN", "HERO_PG_DSN", "HERO_BILLING_DSN", "HERO_BILLING_ALLOW_SHARED_DB", "HERO_PG_HOST")
_SECRET = "p@ss:w/rd%"  # 含 DSN 保留字符，验证百分号编码


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for name in _DSN_ENVS + ("HERO_PG_PASSWORD", "HERO_SECRETS_BACKEND"):
        monkeypatch.delenv(name, raising=False)
    yield


def test_password_injected_when_dsn_has_user_and_no_password(monkeypatch):
    monkeypatch.setenv("HERO_CHECKPOINT_DSN", "postgresql://hero_user@db.internal:5432/heroquant")
    monkeypatch.setenv("HERO_PG_PASSWORD", _SECRET)
    dsn = s._checkpoint_dsn_from_env()
    assert "hero_user" in dsn
    assert ":5432" in dsn
    from urllib.parse import urlsplit

    parts = urlsplit(dsn)
    assert parts.username == "hero_user"
    assert unquote(parts.password or "") == _SECRET
    assert parts.path == "/heroquant"
    assert _SECRET not in dsn  # 必须编码后写入，不能原样出现


def test_explicit_password_wins(monkeypatch):
    explicit = "postgresql://hero_user:explicit-pass@db.internal:5432/heroquant"
    monkeypatch.setenv("HERO_CHECKPOINT_DSN", explicit)
    monkeypatch.setenv("HERO_PG_PASSWORD", _SECRET)
    assert s._checkpoint_dsn_from_env() == explicit


def test_vault_backend_does_not_inject(monkeypatch):
    monkeypatch.setenv("HERO_SECRETS_BACKEND", "vault")
    monkeypatch.setenv("HERO_PG_PASSWORD", _SECRET)
    monkeypatch.setenv("HERO_CHECKPOINT_DSN", "postgresql://hero_user@db.internal:5432/heroquant")
    assert s._checkpoint_dsn_from_env() == "postgresql://hero_user@db.internal:5432/heroquant"


def test_no_user_is_not_injected(monkeypatch):
    # 只有口令没有用户名拼不出合法 DSN：保持原样
    monkeypatch.setenv("HERO_PG_PASSWORD", _SECRET)
    monkeypatch.setenv("HERO_PG_HOST", "localhost")
    assert s._checkpoint_dsn_from_env() == "postgresql://localhost:5432/heroquant"


def test_no_password_keeps_dsn_unchanged(monkeypatch):
    monkeypatch.setenv("HERO_CHECKPOINT_DSN", "postgresql://hero_user@db.internal:5432/heroquant")
    assert s._checkpoint_dsn_from_env() == "postgresql://hero_user@db.internal:5432/heroquant"


def test_query_params_preserved(monkeypatch):
    monkeypatch.setenv("HERO_CHECKPOINT_DSN", "postgresql://hero_user@db.internal:5432/heroquant?sslmode=require")
    monkeypatch.setenv("HERO_PG_PASSWORD", "simple-pass")
    dsn = s._checkpoint_dsn_from_env()
    assert dsn.endswith("?sslmode=require")
    assert ":simple-pass@" in dsn


def test_billing_dsn_also_gets_password(monkeypatch):
    monkeypatch.setenv("HERO_BILLING_DSN", "postgresql://hero_user@db.internal:5432/billing")
    monkeypatch.setenv("HERO_PG_PASSWORD", "billing-pass")
    assert ":billing-pass@" in (s._billing_dsn_from_env() or "")


def test_password_never_logged(monkeypatch, caplog):
    monkeypatch.setenv("HERO_CHECKPOINT_DSN", "postgresql://hero_user@db.internal:5432/heroquant")
    monkeypatch.setenv("HERO_PG_PASSWORD", _SECRET)
    with caplog.at_level(logging.DEBUG):
        s._checkpoint_dsn_from_env()
    assert _SECRET not in caplog.text
    assert "pg_password_injected" in caplog.text


def test_non_pg_scheme_untouched(monkeypatch):
    monkeypatch.setenv("HERO_PG_PASSWORD", _SECRET)
    assert s._apply_pg_password("redis://user@cache:6379/0") == "redis://user@cache:6379/0"
    assert s._apply_pg_password(None) is None

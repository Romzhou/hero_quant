"""W4/W8 存储层离线契约：fail-closed / 入参校验 / 审计脱敏白名单。

这些用例不碰 PG：只验证「无库必须 503」「非法入参在落库前拒绝」「明文敏感字段不得写审计」。
真实 tenant 隔离、幂等与 append-only 由 `tests/test_w4w8_pg.py` 在 PG 守卫下验证。
"""

from __future__ import annotations

import pytest

from hero_quant.audit.store import (
    AuditStore,
    AuditStoreUnavailable,
    AuditValidationError,
)
from hero_quant.session.store import SessionStore, SessionStoreUnavailable


class _NeverPool:
    """任何访问都失败：用于证明校验先于 IO。"""

    def connection(self):  # pragma: no cover - 被调用即测试失败
        raise AssertionError("pool must not be touched for invalid input")


class _DownPool:
    def connection(self):
        raise RuntimeError("pg down")


def test_session_store_requires_pool():
    with pytest.raises(ValueError, match="connection"):
        SessionStore(None)
    with pytest.raises(ValueError, match="connection"):
        SessionStore(object())


def test_session_store_from_empty_dsn_is_503():
    with pytest.raises(SessionStoreUnavailable) as excinfo:
        SessionStore.from_dsn("")
    assert excinfo.value.status_code == 503
    assert excinfo.value.to_dict()["error"] == "sessions_unavailable"


def test_session_store_pool_failure_is_503():
    store = SessionStore(_DownPool())
    with pytest.raises(SessionStoreUnavailable) as excinfo:
        store.create_session("acme", "u1")
    assert excinfo.value.reason == "pool_connection_failed"


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.create_session("", "u1"),
        lambda s: s.create_session("acme", " "),
        lambda s: s.get_session("acme", ""),
        lambda s: s.list_sessions("acme", limit=0),
        lambda s: s.append_message("acme", "sid", "hacker", "x"),
        lambda s: s.start_run("acme", ""),
        lambda s: s.finish_run("acme", "r1", "running"),
        lambda s: s.record_tool_call("acme", "r1", "tool", "hash", "key", status="DONE"),
        lambda s: s.get_tool_call("acme", ""),
    ],
)
def test_session_store_rejects_invalid_input_before_io(call):
    store = SessionStore(_NeverPool())
    with pytest.raises(ValueError):
        call(store)


def test_session_store_health_is_false_when_pool_down():
    assert SessionStore(_DownPool()).health() is False


def test_session_store_health_true_when_select_ok():
    class _Pool:
        def connection(self):
            class _Ctx:
                def __enter__(self_inner):
                    class _Conn:
                        def execute(self, sql):
                            assert sql == "SELECT 1"

                    return _Conn()

                def __exit__(self_inner, *args):
                    return False

            return _Ctx()

    assert SessionStore(_Pool()).health() is True


def test_audit_store_requires_pool():
    with pytest.raises(ValueError, match="connection"):
        AuditStore(None)


def test_audit_store_from_empty_dsn_is_503():
    with pytest.raises(AuditStoreUnavailable) as excinfo:
        AuditStore.from_dsn("")
    assert excinfo.value.status_code == 503


def test_audit_rejects_plaintext_sensitive_detail():
    store = AuditStore(_NeverPool())
    with pytest.raises(AuditValidationError) as excinfo:
        store.append("acme", "llm.call", detail={"prompt": "tell me a secret"})
    assert "detail_contains_sensitive_keys" in str(excinfo.value)
    assert excinfo.value.status_code == 400


def test_audit_rejects_empty_action():
    store = AuditStore(_NeverPool())
    with pytest.raises(AuditValidationError, match="action_required"):
        store.append("acme", "")


def test_audit_rejects_multiline_hash():
    store = AuditStore(_NeverPool())
    with pytest.raises(AuditValidationError, match="prompt_hash_must_be_single_line"):
        store.append("acme", "llm.call", prompt_hash="abc\nforged")


def test_audit_try_append_returns_false_without_raising():
    store = AuditStore(_NeverPool())
    assert store.try_append("acme", "llm.call", detail={"content": "leak"}) is False


def test_audit_detail_accepts_non_sensitive_fields():
    payload = AuditStore._safe_detail({"tool": "get_bars", "count": 3})
    assert '"tool": "get_bars"' in payload

"""PR-151 轮换事件 → 审计链（PR-241）：只带 name/version/fingerprint/state，hook 永不抛。

覆盖：begin/complete 各写一条 / 白名单字段 / 真实轮换流程端到端不漏值 /
chain 抛错时 hook 不抛且 warning 不带异常文本 / 非轮换状态跳过 / 租户覆盖。

假值一律 `replace-with-*` 形态（`gitleaks.toml` 窄口径放行）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from hero_quant.secrets import (
    STATE_ROTATING,
    STATE_STABLE,
    STATE_UNKNOWN,
    begin_rotation,
    complete_rotation,
    fingerprint,
)
from hero_quant.secrets.audit_bridge import (
    ACTION_ROTATION_BEGIN,
    ACTION_ROTATION_COMPLETE,
    DETAIL_KEYS,
    SYSTEM_TENANT,
    rotation_audit_hook,
)

_FAKE_V1 = "replace-with-fake-llm-key-v1"
_FAKE_V2 = "replace-with-fake-llm-key-v2"
_LOGGER = "hero_quant.secrets.audit_bridge"


class _FakeChain:
    """鸭子类型链：只实现 append，便于断言调用与注入异常。"""

    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._error = error

    def append(self, tenant_id: str, action: str, **fields: Any) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        self.calls.append((tenant_id, action, fields))
        return {"id": len(self.calls)}


def _record(state: str, *, name: str = "llm_api_key", version: int = 0, fp: str = "0123456789ab") -> dict[str, Any]:
    """形状与 rotation._status 返回一致（字段集 STATE_KEYS）。"""
    return {
        "name": name,
        "state": state,
        "version": version,
        "rotated_at": None,
        "previous_version": None,
        "persistence": "file",
        "fingerprint": fp,
        "pending_fingerprint": "abcdef012345",
    }


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("HERO_SECRETS_BACKEND", "HERO_API_KEY", "HERO_API_KEY_NEXT"):
        monkeypatch.delenv(name, raising=False)
    yield


def test_begin_writes_one_chained_entry_with_whitelisted_detail():
    chain = _FakeChain()
    hook = rotation_audit_hook(chain)
    assert hook(_record(STATE_ROTATING)) is None
    assert len(chain.calls) == 1
    tenant, action, fields = chain.calls[0]
    assert (tenant, action) == (SYSTEM_TENANT, ACTION_ROTATION_BEGIN)
    assert fields["resource"] == "llm_api_key"
    assert set(fields["detail"]) == set(DETAIL_KEYS), "detail 只允许 name/version/fingerprint/state"
    assert fields["detail"]["state"] == STATE_ROTATING


def test_complete_writes_one_chained_entry():
    chain = _FakeChain()
    hook = rotation_audit_hook(chain)
    hook(_record(STATE_STABLE, version=1, fp="fedcba987654"))
    assert len(chain.calls) == 1
    _, action, fields = chain.calls[0]
    assert action == ACTION_ROTATION_COMPLETE
    assert fields["detail"]["version"] == 1
    assert fields["detail"]["fingerprint"] == "fedcba987654"


def test_real_rotation_flow_never_leaks_secret_values(monkeypatch, tmp_path):
    monkeypatch.setenv("HERO_API_KEY", _FAKE_V1)
    monkeypatch.setenv("HERO_API_KEY_NEXT", _FAKE_V2)
    state_path = tmp_path / "secrets_state.json"
    chain = _FakeChain()
    hook = rotation_audit_hook(chain)

    begun = begin_rotation("llm_api_key", state_path=state_path)
    hook(begun)
    monkeypatch.setenv("HERO_API_KEY", _FAKE_V2)  # 操作员把新值提升到主槽
    monkeypatch.delenv("HERO_API_KEY_NEXT", raising=False)
    done = complete_rotation("llm_api_key", state_path=state_path)
    hook(done)

    assert [call[1] for call in chain.calls] == [ACTION_ROTATION_BEGIN, ACTION_ROTATION_COMPLETE]
    assert done["version"] == 1 and begun["version"] == 0
    dumped = json.dumps([call[2] for call in chain.calls], ensure_ascii=False)
    assert _FAKE_V1 not in dumped and _FAKE_V2 not in dumped, "审计链里绝不能出现密钥值"
    # begin 记录的 fingerprint 是「当前槽」（v1）；complete 是提升后的新值（v2）
    assert chain.calls[0][2]["detail"]["fingerprint"] == fingerprint(_FAKE_V1)
    assert chain.calls[1][2]["detail"]["fingerprint"] == fingerprint(_FAKE_V2)


@pytest.mark.parametrize("exc", [RuntimeError("chain down"), ValueError("bad field"), ConnectionError("pool closed")])
def test_chain_failure_is_swallowed_with_warning(exc, caplog):
    hook = rotation_audit_hook(_FakeChain(error=exc))
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    assert hook(_record(STATE_STABLE, version=1)) is None, "审计失败不许冒到轮换流程"
    assert "secrets.rotation_audit_failed" in caplog.text
    assert "rotation_audit_failed" in caplog.text


def test_chain_failure_message_is_not_logged(caplog):
    class _LeakyChain:
        def append(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(f"boom value={_FAKE_V1}")

    hook = rotation_audit_hook(_LeakyChain())
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    hook(_record(STATE_STABLE, version=1))
    assert _FAKE_V1 not in caplog.text, "异常文本可能带值，日志只记异常类型"


@pytest.mark.parametrize("state", [STATE_UNKNOWN, "", "pending", None])
def test_non_transition_state_is_skipped(state):
    chain = _FakeChain()
    event = _record(STATE_ROTATING)
    event["state"] = state
    rotation_audit_hook(chain)(event)
    assert chain.calls == []


def test_non_mapping_event_is_skipped_with_warning(caplog):
    chain = _FakeChain()
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    assert rotation_audit_hook(chain)(["not", "a", "dict"]) is None
    assert chain.calls == []
    assert "rotation_event_invalid" in caplog.text


def test_missing_name_is_skipped_with_warning(caplog):
    chain = _FakeChain()
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    event = _record(STATE_ROTATING)
    event["name"] = ""
    rotation_audit_hook(chain)(event)
    assert chain.calls == []
    assert "rotation_name_missing" in caplog.text


def test_tenant_can_be_overridden_by_event():
    chain = _FakeChain()
    event = _record(STATE_ROTATING) | {"tenant_id": "tenant-a"}
    rotation_audit_hook(chain)(event)
    assert chain.calls[0][0] == "tenant-a"

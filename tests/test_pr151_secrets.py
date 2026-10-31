"""PR-151 密钥托管（B10-B）定向测试：env 后端 / fail-closed / 脱敏 / 轮换记账 / 三处联动。

假值一律 `replace-with-*` 形态（`gitleaks.toml` 窄口径放行），绝不读取 .env 真值。
"""

from __future__ import annotations

import json
import logging
import pathlib

import pytest

from hero_quant.secrets import (
    STATE_KEYS,
    STATE_ROTATING,
    STATE_STABLE,
    STATE_UNKNOWN,
    EnvSecretsProvider,
    SecretRotationError,
    UnsupportedSecretsBackendError,
    begin_rotation,
    complete_rotation,
    fingerprint,
    get_secrets_provider,
    rotation_status,
)
from hero_quant.secrets.env_provider import SECRET_ENV_VARS

# gitleaks 放行的占位形态假值（见 gitleaks.toml [[allowlists]]）
FAKE_V1 = "replace-with-fake-llm-key-v1"
FAKE_V2 = "replace-with-fake-llm-key-v2"


@pytest.fixture(autouse=True)
def _clean_backend(monkeypatch):
    """每个用例从干净后端名出发，避免相互污染。"""
    monkeypatch.delenv("HERO_SECRETS_BACKEND", raising=False)


# ---------- 1. env 后端：命中 / 缺失 / 未知 ----------

def test_env_provider_hit_miss_and_unknown(monkeypatch):
    monkeypatch.setenv("HERO_API_KEY", FAKE_V1)
    monkeypatch.delenv("COHERE_API_KEY", raising=False)
    p = get_secrets_provider()
    assert p.backend == "env"
    got = p.get_secret("llm_api_key")
    assert got is not None and got.reveal() == FAKE_V1 and got.slot == "current"
    assert p.get_secret("cohere_api_key") is None, "未配置必须返回 None（不抛、不造值）"
    assert p.get_secret("no_such_secret") is None, "未知 name 必须返回 None"


def test_env_provider_names_and_capabilities():
    p = EnvSecretsProvider()
    assert set(p.names()) == set(SECRET_ENV_VARS)
    cap = p.capabilities()
    assert cap["rotation"] == "manual", "env 后端是人工轮换，不许声称自动"
    assert cap["persistence"] == "env_file", "必须如实声明明文落 .env"
    assert cap["degraded"] is False, "env 是当前唯一真实后端，不算降级"
    assert p.env_var("llm_api_key", slot="next") == "HERO_API_KEY_NEXT", "双读槽位用 <VAR>_NEXT"


# ---------- 2. fail-closed：vault/kms 不静默回落 env ----------

def test_vault_backend_is_explicitly_unavailable(monkeypatch):
    monkeypatch.setenv("HERO_SECRETS_BACKEND", "vault")
    monkeypatch.setenv("HERO_API_KEY", FAKE_V1)
    with pytest.raises(UnsupportedSecretsBackendError) as exc:
        get_secrets_provider()
    msg = str(exc.value)
    assert "vault" in msg
    assert FAKE_V1 not in msg, "异常信息不得回显密钥值"


def test_kms_backend_is_explicitly_unavailable(monkeypatch):
    monkeypatch.setenv("HERO_SECRETS_BACKEND", "kms")
    with pytest.raises(UnsupportedSecretsBackendError):
        get_secrets_provider()


def test_unknown_backend_raises_value_error():
    with pytest.raises(ValueError):
        get_secrets_provider("not-a-backend")


def test_settings_unknown_backend_fail_closed(monkeypatch):
    from hero_quant.config.settings import Settings

    monkeypatch.setenv("HERO_SECRETS_BACKEND", "not-a-backend")
    with pytest.raises(ValueError):
        Settings()


# ---------- 3. 脱敏：repr / 日志 / describe 不含真值 ----------

def test_secret_value_repr_and_describe_never_leak(monkeypatch):
    monkeypatch.setenv("HERO_API_KEY", FAKE_V1)
    sv = get_secrets_provider().get_secret("llm_api_key")
    text = " ".join([repr(sv), str(sv), str(sv.as_log_dict()), str(get_secrets_provider().describe())])
    assert FAKE_V1 not in text, "repr/str/log_dict/describe 不得出现真值"
    assert sv.fingerprint == fingerprint(FAKE_V1) and sv.fingerprint in repr(sv)
    assert str(sv.length) in repr(sv), "脱敏形态保留长度，便于运维核对"


def test_rotation_log_never_leaks_secret(caplog, monkeypatch, tmp_path):
    monkeypatch.setenv("HERO_API_KEY", FAKE_V1)
    monkeypatch.setenv("HERO_API_KEY_NEXT", FAKE_V2)
    state = tmp_path / "secrets_state.json"
    with caplog.at_level(logging.INFO, logger="hero_quant.secrets.rotation"):
        begin_rotation("llm_api_key", state_path=state)
        monkeypatch.setenv("HERO_API_KEY", FAKE_V2)  # 模拟运维把新值切到主槽
        complete_rotation("llm_api_key", state_path=state)
    assert caplog.text, "轮换事件必须留下可审计日志"
    assert FAKE_V1 not in caplog.text and FAKE_V2 not in caplog.text


# ---------- 4. 轮换状态机 ----------

def test_rotation_lifecycle_and_state_file_has_no_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("HERO_API_KEY", FAKE_V1)
    monkeypatch.setenv("HERO_API_KEY_NEXT", FAKE_V2)
    state = tmp_path / "secrets_state.json"

    fresh = rotation_status("llm_api_key", state_path=state)
    assert fresh["state"] == STATE_UNKNOWN and fresh["version"] == 0 and fresh["persistence"] == "file"

    rotating = begin_rotation("llm_api_key", state_path=state)
    assert rotating["state"] == STATE_ROTATING
    assert rotating["version"] == 0, "开窗口不动版本"
    assert rotating["pending_fingerprint"] == fingerprint(FAKE_V2)

    with pytest.raises(SecretRotationError):
        complete_rotation("llm_api_key", state_path=state)  # 主槽还没切 → 拒绝记账

    monkeypatch.setenv("HERO_API_KEY", FAKE_V2)
    done = complete_rotation("llm_api_key", state_path=state)
    assert done["state"] == STATE_STABLE
    assert done["version"] == 1 and done["previous_version"] == 0
    assert done["rotated_at"] and done["fingerprint"] == fingerprint(FAKE_V2)

    body = state.read_text(encoding="utf-8")
    assert FAKE_V1 not in body and FAKE_V2 not in body, "状态文件不得含任何密钥值"
    assert set(json.loads(body)["llm_api_key"]) == set(STATE_KEYS), "状态字段不得超出白名单"


def test_begin_rotation_requires_next_slot(monkeypatch, tmp_path):
    monkeypatch.setenv("HERO_API_KEY", FAKE_V1)
    monkeypatch.delenv("HERO_API_KEY_NEXT", raising=False)
    with pytest.raises(SecretRotationError):
        begin_rotation("llm_api_key", state_path=tmp_path / "s.json")


def test_begin_rotation_refuses_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("HERO_API_KEY", FAKE_V1)
    monkeypatch.setenv("HERO_API_KEY_NEXT", FAKE_V1)
    with pytest.raises(SecretRotationError):
        begin_rotation("llm_api_key", state_path=tmp_path / "s.json")


def test_complete_rotation_without_begin_is_refused(tmp_path):
    with pytest.raises(SecretRotationError):
        complete_rotation("llm_api_key", state_path=tmp_path / "s.json")


def test_persistence_memory_is_disclosed(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not-a-dir", encoding="utf-8")
    st = rotation_status("llm_api_key", state_path=blocker / "secrets_state.json")
    assert st["persistence"] == "memory", "写不进去必须如实显示 memory，不假装落盘"


# ---------- 5. 三处联动 + env gate 不扩散 ----------

def test_env_docs_three_way_linkage(monkeypatch):
    from hero_quant.config.settings import Settings

    monkeypatch.delenv("HERO_SECRETS_BACKEND", raising=False)
    assert Settings().secrets_backend == "env", "settings 默认必须是 env"
    assert "HERO_SECRETS_BACKEND=env" in pathlib.Path(".env.example").read_text(encoding="utf-8")
    assert "| `HERO_SECRETS_BACKEND` | `env` |" in pathlib.Path("README.md").read_text(encoding="utf-8")
    doc = pathlib.Path("docs/SECURITY-SECRETS.md").read_text(encoding="utf-8")
    assert "未覆盖范围" in doc and "明文" in doc, "runbook 必须写明未覆盖范围"


def test_secrets_package_has_no_direct_getenv():
    """env gate 不扩散：secrets 包经 config.settings 读 env（与 test_config.py 同口径）。"""
    for p in pathlib.Path("src/hero_quant/secrets").glob("*.py"):
        assert "os.getenv(" not in p.read_text(encoding="utf-8"), f"raw getenv in {p}"

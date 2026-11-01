"""批次14 接线：Redis 口令经 secrets provider 注入 DSN + 轮换事件旁路到审计。

这两处是主控在批次 4 做的最小接线，不改变既有降级语义：
- env 后端：口令照旧可用（百分号编码进 DSN）；
- vault/kms：取不到口令就保持无口令，绝不回落 os.environ；
- 轮换钩子：只把状态字典（name/state/version/fingerprint）交出去，值不出 rotation。
"""

from __future__ import annotations

import os

import pytest

import hero_quant.config.settings as settings_mod
from hero_quant.secrets.env_provider import EnvSecretsProvider
from hero_quant.secrets.rotation import begin_rotation, complete_rotation


@pytest.fixture()
def _clean_redis_env(monkeypatch):
    # settings 导入时就跑过 dotenv，故必须在导入之后清键（否则 .env 会盖掉本用例的设置）
    for key in ("HERO_REDIS_DSN", "REDIS_URL", "HERO_SECRETS_BACKEND", "HERO_REDIS_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    yield monkeypatch


def test_redis_env_backend_encodes_password(_clean_redis_env):
    monkeypatch = _clean_redis_env
    monkeypatch.setenv("HERO_REDIS_HOST", "cache.internal")
    monkeypatch.setenv("HERO_REDIS_PASSWORD", "p@ss:w/rd")
    dsn = settings_mod._redis_dsn_from_env()
    assert "cache.internal" in dsn
    assert "%40" in dsn and "%3A" in dsn and "%2F" in dsn
    assert "p@ss" not in dsn  # 原文不得出现


@pytest.mark.parametrize("backend", ["vault", "kms"])
def test_redis_unimplemented_backend_does_not_fall_back(_clean_redis_env, backend):
    monkeypatch = _clean_redis_env
    monkeypatch.setenv("HERO_SECRETS_BACKEND", backend)
    monkeypatch.setenv("HERO_REDIS_HOST", "cache.internal")
    monkeypatch.setenv("HERO_REDIS_PASSWORD", "p@ss:w/rd")
    dsn = settings_mod._redis_dsn_from_env()
    authority = dsn.split("://", 1)[1].split("/")[0]
    assert "@" not in authority
    assert "%40" not in dsn


def test_redis_dsn_password_injected_when_userinfo_has_no_password(_clean_redis_env):
    monkeypatch = _clean_redis_env
    monkeypatch.setenv("HERO_REDIS_DSN", "redis://user@cache.internal:6379/1")
    monkeypatch.setenv("HERO_REDIS_PASSWORD", "s3cr3t")
    dsn = settings_mod._redis_dsn_from_env()
    assert "user:s3cr3t@cache.internal:6379/1" in dsn


def _rotation_env(monkeypatch, current: str, pending: str) -> EnvSecretsProvider:
    monkeypatch.setenv("HERO_API_KEY", current)
    monkeypatch.setenv("HERO_API_KEY_NEXT", pending)
    monkeypatch.delenv("HERO_SECRETS_BACKEND", raising=False)
    return EnvSecretsProvider()


def test_rotation_emits_audit_events_without_secret_values(monkeypatch, tmp_path):
    provider = _rotation_env(monkeypatch, "old-value-0001", "new-value-0002")
    state = tmp_path / "secrets_state.json"
    events: list[dict] = []

    begin = begin_rotation("llm_api_key", provider=provider, state_path=state, audit_hook=events.append)
    assert begin["state"] == "rotating"
    assert len(events) == 1 and events[0]["state"] == "rotating"

    # 模拟运维把新值提升到主槽
    monkeypatch.setenv("HERO_API_KEY", "new-value-0002")
    done = complete_rotation("llm_api_key", provider=provider, state_path=state, audit_hook=events.append)
    assert done["state"] == "stable" and done["version"] == 1
    assert len(events) == 2 and events[1]["state"] == "stable"

    dumped = repr(events)
    assert "old-value-0001" not in dumped
    assert "new-value-0002" not in dumped
    assert all("fingerprint" in item for item in events)


def test_rotation_survives_broken_audit_hook(monkeypatch, tmp_path):
    provider = _rotation_env(monkeypatch, "old-value-0001", "new-value-0002")
    state = tmp_path / "secrets_state.json"

    def boom(_event: dict) -> None:
        raise RuntimeError("audit down (error text may contain old-value-0001)")

    result = begin_rotation("llm_api_key", provider=provider, state_path=state, audit_hook=boom)
    assert result["state"] == "rotating"  # 审计失败不许改变轮换结果

# --------------------------------------------------------------------------- CLI 入口

def test_worker_cli_requires_explicit_handler():
    """没有 --handler 必须 exit 2：空处理器会领走任务再失败，烧掉 max_tries。"""
    import subprocess
    import sys

    proc = subprocess.run([sys.executable, "-m", "hero_quant.runs"], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2
    assert "missing --handler" in proc.stderr


def test_worker_cli_help_works():
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "hero_quant.runs", "--help"], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0
    assert "--handler" in proc.stdout and "--once" in proc.stdout

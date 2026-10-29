"""PR-173 kill switch：作用域优先级 / 503 reason / fail-closed / 审计联动。

用 fakeredis 隔离外部依赖；每个用例独立 client，避免键串扰。
"""

from __future__ import annotations

import asyncio

import pytest

fakeredis_async = pytest.importorskip("fakeredis.aioredis")

from hero_quant.killswitch import (  # noqa: E402 - importorskip 之后才可导入依赖链
    KillSwitch,
    KillSwitchActive,
    KillSwitchUnavailable,
    KillSwitchValidationError,
)

import hero_quant.killswitch.switch as switch_module  # noqa: E402


def _client():
    return fakeredis_async.FakeRedis(decode_responses=True)


class _Audit:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[tuple[tuple, dict]] = []

    def append(self, *args, **kwargs):
        if self.fail:
            raise RuntimeError("audit db down")
        self.calls.append((args, kwargs))


def test_tool_kill_returns_503_with_explicit_reason():
    async def case():
        client = _client()
        ks = KillSwitch(redis=client)
        await ks.enable("tool", "get_bars", "provider outage", actor="ops")
        with pytest.raises(KillSwitchActive) as excinfo:
            await ks.check_tool("get_bars")
        payload = excinfo.value.to_dict()
        assert excinfo.value.status_code == 503
        assert payload == {
            "ok": False,
            "error": "killed",
            "scope": "tool",
            "target": "get_bars",
            "tenant_id": None,
            "reason": "provider outage",
        }

    asyncio.run(case())


def test_disable_restores_capability():
    async def case():
        client = _client()
        ks = KillSwitch(redis=client)
        await ks.enable("model", "gpt-4o", "cost spike", actor="ops")
        assert await ks.disable("model", "gpt-4o", actor="ops") is True
        await ks.check_model("gpt-4o")  # 不再抛错
        assert await ks.state("model", "gpt-4o") is None
        assert await ks.disable("model", "gpt-4o", actor="ops") is False

    asyncio.run(case())


def test_tenant_override_only_affects_that_tenant():
    async def case():
        client = _client()
        ks = KillSwitch(redis=client)
        await ks.enable("tool", "run_backtest", "abuse", actor="ops", tenant_id="acme")
        with pytest.raises(KillSwitchActive):
            await ks.check_tool("run_backtest", tenant_id="acme")
        await ks.check_tool("run_backtest", tenant_id="globex")  # 未命中
        # 全局开关对两个租户都生效
        await ks.enable("tool", "run_backtest", "global maintenance", actor="ops")
        with pytest.raises(KillSwitchActive):
            await ks.check_tool("run_backtest", tenant_id="globex")

    asyncio.run(case())


def test_tenant_scope_blocks_whole_tenant():
    async def case():
        client = _client()
        ks = KillSwitch(redis=client)
        await ks.enable("tenant", "acme", "contract terminated", actor="ops")
        with pytest.raises(KillSwitchActive) as excinfo:
            await ks.enforce("acme", model="gpt-4o", tool="get_bars")
        assert excinfo.value.scope == "tenant"

    asyncio.run(case())


def test_enforce_checks_model_and_tool():
    async def case():
        client = _client()
        ks = KillSwitch(redis=client)
        await ks.enable("model", "gpt-4o", "deprecated", actor="ops")
        with pytest.raises(KillSwitchActive) as excinfo:
            await ks.enforce("acme", model="gpt-4o", tool="get_bars")
        assert excinfo.value.scope == "model"

    asyncio.run(case())


def test_list_active_returns_enabled_switches():
    async def case():
        client = _client()
        ks = KillSwitch(redis=client)
        await ks.enable("tool", "a", "r1", actor="ops")
        await ks.enable("model", "b", "r2", actor="ops")
        active = await ks.list_active()
        assert {(item["scope"], item["target"]) for item in active} == {("tool", "a"), ("model", "b")}
        assert [item["target"] for item in await ks.list_active("tool")] == ["a"]

    asyncio.run(case())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"scope": "database", "target": "x", "reason": "r", "actor": "ops"},
        {"scope": "tool", "target": "a:b", "reason": "r", "actor": "ops"},
        {"scope": "tool", "target": "x", "reason": "", "actor": "ops"},
        {"scope": "tool", "target": "x", "reason": "r", "actor": ""},
        {"scope": "tool", "target": "x", "reason": "r", "actor": "ops", "ttl_seconds": 0},
    ],
)
def test_invalid_input_is_400(kwargs):
    async def case():
        ks = KillSwitch(redis=_client())
        with pytest.raises(KillSwitchValidationError) as excinfo:
            await ks.enable(**kwargs)
        assert excinfo.value.status_code == 400

    asyncio.run(case())


def test_redis_unavailable_is_fail_closed(monkeypatch):
    async def fake_get_redis():
        return None

    monkeypatch.setattr(switch_module, "get_redis", fake_get_redis)

    async def case():
        ks = KillSwitch()
        with pytest.raises(KillSwitchUnavailable) as excinfo:
            await ks.check_tool("get_bars")
        assert excinfo.value.status_code == 503
        with pytest.raises(KillSwitchUnavailable):
            await ks.enable("tool", "get_bars", "r", actor="ops")

    asyncio.run(case())


def test_corrupt_payload_fails_closed():
    async def case():
        client = _client()
        ks = KillSwitch(redis=client)
        await client.set("hero:killswitch:tool:-:get_bars", "{not-json")
        with pytest.raises(KillSwitchUnavailable, match="payload_corrupt"):
            await ks.check_tool("get_bars")

    asyncio.run(case())


def test_enable_writes_audit():
    async def case():
        audit = _Audit()
        ks = KillSwitch(redis=_client(), audit=audit)
        await ks.enable("tool", "get_bars", "outage", actor="ops", tenant_id="acme")
        assert len(audit.calls) == 1
        args, kwargs = audit.calls[0]
        assert args[0] == "acme"
        assert args[1] == "killswitch.enable"
        assert kwargs["detail"]["kill_reason"] == "outage"

    asyncio.run(case())


def test_audit_failure_rolls_back_switch():
    async def case():
        client = _client()
        ks = KillSwitch(redis=client, audit=_Audit(fail=True))
        with pytest.raises(RuntimeError, match="audit db down"):
            await ks.enable("tool", "get_bars", "outage", actor="ops")
        # 审计失败不能留下「已停用但无记录」的开关
        assert await ks.state("tool", "get_bars") is None

    asyncio.run(case())

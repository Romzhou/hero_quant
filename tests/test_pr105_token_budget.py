"""PR-105 余项（租户日 token 预算真正记账）+ PR-115（trace/响应的成本字段）。

覆盖口径（全部 fail-closed，不连真 Redis / 不发真 LLM 调用）：
1. 预算用尽 → **跑循环之前** 429（`QuotaExceeded` + `Retry-After` 到 UTC 零点），且不调用 loop；
2. 正常跑完 → `consume_tokens` 收到后端 `total_tokens`，响应含 `llm_cost`，trace.jsonl 落 `llm_cost`；
3. 跑完才超额 → 200 + degraded=true + 稳定 reason，**已完成的答案不丢**（不改成 429）；
4. Redis 不可用 → 显式 degraded `quota_backend_unavailable` + 指标计数，绝不静默通过；
5. `_FakeLLM`（无 key）→ 不扣预算、响应里不出现 `llm_cost`（本文件锁定的取舍）；
6. 目录外模型 → `cost_usd=None`（不猜价、不算 0）；
7. `LoopResult.llm_usage` 只透出后端真实累计（全 0 保持 None，不用字符估算凑数）。

测试装配说明：端点直接按协程调用（同 tests/test_server_auth_wiring.py 的风格），
限流/ticket/kill switch/checkpoint/LLMFactory/AgentLoop 全部替换为替身；429 的 HTTP 映射
由 `auth_error_response`（统一处理器本体）断言，端点层 429+Retry-After 已在
`tests/test_server_auth_wiring.py::test_quota_exceeded_maps_to_429_with_retry_after` 锁定。
"""

from __future__ import annotations

import asyncio
import json
import types

import fakeredis.aioredis as fakeredis_async
import pytest
from fastapi import BackgroundTasks
from fastapi.responses import JSONResponse

import hero_quant.api.server as srv
import hero_quant.auth.quota as quota_mod
from hero_quant.agent.loop import AgentLoop, LoopResult
from hero_quant.auth.context import bind_principal
from hero_quant.auth.deps import auth_error_response
from hero_quant.auth.errors import AuthConfigError, QuotaExceeded
from hero_quant.auth.principal import Principal, Role
from hero_quant.auth.quota import QuotaPolicy, TenantQuotaLimiter

_GPT_MINI = "gpt-4o-mini"
_UNPRICED = "acme-super-model"


# ---------------------------------------------------------------------------
# 基础设施替身
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_redis():
    """异步 fakeredis（同 tests/test_auth_quota.py 的口径，不连真 Redis）。"""
    import hero_quant.infra.redis as rmod

    rmod.clear_redis_instance()
    fake = fakeredis_async.FakeRedis(decode_responses=True)
    rmod.set_redis_instance(fake, for_async=True)
    try:
        yield fake
    finally:
        rmod.clear_redis_instance()


class _FakeSettings:
    """Settings 替身：api_key 非空 = 真实链路；置空即退化到 _FakeLLM 合成路径。"""

    api_key = "test-key"
    openai_api_key = ""
    llm_model = _GPT_MINI
    llm_provider = "openai"
    wall_time_budget = 30.0
    wall_time_budget_seconds = None


class _StubFactory:
    """LLMFactory 替身：不发网络请求；provider/model 由类属性控制（便于逐测试改写）。"""

    provider_name = "openai"
    model = _GPT_MINI

    def __init__(self, settings=None, **kwargs):
        self.provider = self.provider_name

    def model_for_stage(self, stage):  # noqa: ARG002 — 端点只取 .name
        return types.SimpleNamespace(name=type(self).model)

    def create(self, **kwargs):  # noqa: ARG002 — 返回非 LLMClient，端点自会包一层
        return object()


class _StubLoop:
    """AgentLoop 替身：记录调用次数，返回可配置的 LoopResult（含后端 usage）。"""

    calls = 0
    text = "最终答案：600519 收盘 1680.0（工具结果摘要）"
    usage = {"input_tokens": 1000, "output_tokens": 500}

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def run(self, goal):  # noqa: ARG002
        type(self).calls += 1
        return LoopResult(
            terminated=True,
            iterations=1,
            text=type(self).text,
            reason="completed",
            llm_usage=type(self).usage,
        )


class _RealSaver:
    """真实 PG saver 替身：只为让 `_is_emulated_saver` 返回非降级（避免噪音 reason）。"""

    def _is_real_pg_pool(self) -> bool:
        return True


def _principal(tenant: str = "tenant-a") -> Principal:
    return Principal(subject="user-1", tenant_id=tenant, roles=frozenset({Role.ANALYST}))


def _request() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        client=types.SimpleNamespace(host="testclient"),
        headers={},
        state=types.SimpleNamespace(),
    )


def _install_quota(monkeypatch, *, budget: int) -> TenantQuotaLimiter:
    """把进程级默认配额器换成受控预算的实例（monkeypatch 负责还原）。"""
    limiter = TenantQuotaLimiter(QuotaPolicy(requests_per_minute=10_000, daily_token_budget=budget))
    monkeypatch.setattr(quota_mod, "_default_limiter", limiter, raising=False)
    return limiter


@pytest.fixture()
def api_env(monkeypatch, fake_redis):
    """端点跑得动所需的最小替身集合（限流/ticket/killswitch/checkpoint/gateway 探测）。"""
    import hero_quant.config.settings as settings_mod

    monkeypatch.setattr(settings_mod, "Settings", _FakeSettings)
    monkeypatch.setattr("hero_quant.llm.factory.LLMFactory", _StubFactory)
    monkeypatch.setattr("hero_quant.agent.loop.AgentLoop", _StubLoop)
    monkeypatch.setattr(srv, "_check_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(srv, "consume_ticket_with_user", lambda *a, **k: (True, None))
    monkeypatch.setattr(srv, "consume_ticket", lambda *a, **k: True)
    monkeypatch.setattr(srv, "_get_checkpoint_saver", lambda: _RealSaver())
    monkeypatch.setattr(srv, "_gateway_degraded_reason", lambda: None)
    monkeypatch.setattr(srv, "_get_shadow_stub", lambda: None)

    async def _noop_kill_switch(*a, **k):
        return None

    monkeypatch.setattr(srv, "_enforce_kill_switch", _noop_kill_switch)
    monkeypatch.setattr(_StubFactory, "provider_name", "openai")
    monkeypatch.setattr(_StubFactory, "model", _GPT_MINI)
    monkeypatch.setattr(_StubLoop, "calls", 0)
    monkeypatch.setattr(_StubLoop, "usage", {"input_tokens": 1000, "output_tokens": 500})
    return _StubLoop


def _as_dict(result) -> tuple[dict, dict]:
    """端点返回值可能是 dict（正常）或 JSONResponse（降级走响应头）——统一取 body/headers。"""
    if isinstance(result, JSONResponse):
        return json.loads(result.body), dict(result.headers)
    return result, {}


def _read_trace(trace_dir) -> list[dict]:
    path = trace_dir / "trace.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


async def _call_query(**kwargs) -> object:
    return await srv.query(request=_request(), background_tasks=BackgroundTasks(), q="600519 怎么样", **kwargs)


async def _call_stream(**kwargs) -> list[str]:
    resp = await srv.query_stream(request=_request(), background_tasks=BackgroundTasks(), q="600519 怎么样", **kwargs)
    return [chunk async for chunk in resp.body_iterator]


def _sse_events(chunks: list[str]) -> list[dict]:
    events: list[dict] = []
    for chunk in chunks:
        text = chunk.strip()
        if not text.startswith("data: "):
            continue
        payload = text[len("data: "):]
        if payload == "[DONE]":
            continue
        events.append(json.loads(payload))
    return events


# ---------------------------------------------------------------------------
# 1. 预算用尽 → 跑循环前 429，且不调用 loop
# ---------------------------------------------------------------------------


def test_budget_exhausted_rejects_before_loop_and_maps_to_429(api_env, monkeypatch):
    limiter = _install_quota(monkeypatch, budget=1000)

    async def run():
        await limiter.consume_tokens("tenant-a", 1000)  # 当日额度用满
        with bind_principal(_principal("tenant-a")):
            with pytest.raises(QuotaExceeded) as exc:
                await _call_query()
        return exc.value

    error = asyncio.run(run())

    # 稳定原因走 QuotaExceeded 的 message（与 auth/quota.py:121 同口径），code 仍是 quota_exceeded
    assert str(error) == "tenant_daily_token_budget_exceeded"
    assert error.retry_after >= 1
    assert api_env.calls == 0, "预算闸必须在 AgentLoop.run 之前短路"
    # 统一处理器映射：429 + Retry-After（与 install_auth_error_handlers 同一函数）
    resp = auth_error_response(error)
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == str(error.retry_after)
    assert json.loads(resp.body)["detail"] == "tenant_daily_token_budget_exceeded"


def test_stream_budget_exhausted_rejects_before_stream_starts(api_env, monkeypatch):
    limiter = _install_quota(monkeypatch, budget=500)

    async def run():
        await limiter.consume_tokens("tenant-a", 500)
        with bind_principal(_principal("tenant-a")):
            with pytest.raises(QuotaExceeded) as exc:
                await _call_stream()
        return exc.value

    error = asyncio.run(run())
    assert str(error) == "tenant_daily_token_budget_exceeded"
    assert api_env.calls == 0
    assert auth_error_response(error).status_code == 429


# ---------------------------------------------------------------------------
# 2. 正常跑完 → 记账 total_tokens + 响应/trace 双写 llm_cost
# ---------------------------------------------------------------------------


def test_success_consumes_total_tokens_and_reports_cost(api_env, monkeypatch, tmp_path):
    limiter = _install_quota(monkeypatch, budget=100_000)
    trace_dir = tmp_path / "trace"

    async def run():
        with bind_principal(_principal("tenant-a")):
            result = await _call_query(trace_dir=str(trace_dir))
        return result, await limiter.usage("tenant-a")

    result, used = asyncio.run(run())
    body, headers = _as_dict(result)

    assert body["text"] == api_env.text, "答案不得被记账改写"
    assert "degraded" not in body, f"干净链路不该被标降级: {body.get('degraded_reason')}"
    assert headers.get("x-degraded") is None
    cost = body["llm_cost"]
    assert cost["input_tokens"] == 1000 and cost["output_tokens"] == 500
    assert cost["total_tokens"] == 1500
    assert cost["provider"] == "openai" and cost["model"] == _GPT_MINI
    assert cost["estimated"] is False, "后端给了 usage 就不许标估算"
    assert cost["cost_usd"] == pytest.approx(1000 / 1e6 * 0.15 + 500 / 1e6 * 0.60)
    assert used == 1500, "扣减金额必须等于后端口径 total_tokens"

    records = _read_trace(trace_dir)
    cost_lines = [r for r in records if r.get("type") == "llm_cost"]
    assert len(cost_lines) == 1
    assert cost_lines[0]["total_tokens"] == 1500
    assert cost_lines[0]["provider"] == "openai"


def test_stream_success_consumes_tokens_and_emits_cost_event(api_env, monkeypatch, tmp_path):
    limiter = _install_quota(monkeypatch, budget=100_000)

    async def run():
        with bind_principal(_principal("tenant-a")):
            chunks = await _call_stream(trace_dir=str(tmp_path / "stream_trace"))
        return chunks, await limiter.usage("tenant-a")

    chunks, used = asyncio.run(run())
    events = _sse_events(chunks)

    assert not [e for e in events if e.get("type") == "degraded"], "干净链路的流不该发 degraded 事件"
    cost_events = [e for e in events if e.get("type") == "llm_cost"]
    assert len(cost_events) == 1, "最终 payload 必须带一条 llm_cost"
    assert cost_events[0]["total_tokens"] == 1500
    assert cost_events[0]["degraded"] is False
    assert used == 1500


# ---------------------------------------------------------------------------
# 3. 跑完才超额 → 200 + degraded + reason，答案不丢
# ---------------------------------------------------------------------------


def test_over_budget_after_run_keeps_answer_and_degrades(api_env, monkeypatch):
    limiter = _install_quota(monkeypatch, budget=1000)  # 本次用量 1500 > 1000

    async def run():
        with bind_principal(_principal("tenant-a")):
            result = await _call_query()
        return result, await limiter.usage("tenant-a")

    result, used = asyncio.run(run())
    body, headers = _as_dict(result)

    assert body["text"] == api_env.text, "钱已经花了：不许把已完成的答案改成 429/空响应"
    assert body["degraded"] is True
    assert "tenant_daily_token_budget_exceeded" in body["degraded_reason"]
    assert headers.get("x-degraded") == "true"
    assert body["llm_cost"]["total_tokens"] == 1500, "成本照样要披露"
    assert used == 0, "超额必须回滚，不虚增当日用量"


def test_stream_over_budget_after_run_emits_degraded_event(api_env, monkeypatch, tmp_path):
    limiter = _install_quota(monkeypatch, budget=1000)

    async def run():
        with bind_principal(_principal("tenant-a")):
            chunks = await _call_stream(trace_dir=str(tmp_path / "over_trace"))
        return chunks, await limiter.usage("tenant-a")

    chunks, used = asyncio.run(run())
    events = _sse_events(chunks)

    degraded = [e for e in events if e.get("type") == "degraded"]
    assert degraded, "已开始推送后超额必须发 degraded 事件，不能假装没发生"
    assert "tenant_daily_token_budget_exceeded" in degraded[0]["reason"]
    cost_events = [e for e in events if e.get("type") == "llm_cost"]
    assert cost_events and cost_events[-1]["degraded"] is True
    assert "tenant_daily_token_budget_exceeded" in (cost_events[-1]["degraded_reason"] or "")
    assert used == 0
    # 正文仍要发出去（不能因为超额把答案吞掉）
    assert any("delta" in e for e in events)


# ---------------------------------------------------------------------------
# 4. Redis 不可用 → 显式 degraded（不是静默通过）
# ---------------------------------------------------------------------------


def test_quota_backend_down_degrades_explicitly(api_env, monkeypatch):
    _install_quota(monkeypatch, budget=100_000)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(srv, "_inc_api_error", lambda endpoint, code: calls.append((endpoint, code)))

    async def _no_redis():
        return None

    monkeypatch.setattr(quota_mod, "get_redis", _no_redis)

    async def run():
        with bind_principal(_principal("tenant-a")):
            return await _call_query()

    result = asyncio.run(run())
    body, _ = _as_dict(result)

    assert body["text"] == api_env.text, "记账后端挂了不许吞答案"
    assert body["degraded"] is True
    assert "quota_backend_unavailable" in body["degraded_reason"]
    assert ("query", "quota_backend_unavailable") in calls, "后端不可用必须有指标计数，不许静默"


def test_quota_backend_down_raises_auth_config_inside_helper(monkeypatch):
    """helper 层兜底：Redis 缺失时 `consume_tokens` 抛 AuthConfigError，必须被转成显式 reason。"""
    _install_quota(monkeypatch, budget=100_000)

    async def _no_redis():
        return None

    monkeypatch.setattr(quota_mod, "get_redis", _no_redis)
    reasons: list[str] = []
    counted: list[tuple[str, str]] = []
    monkeypatch.setattr(srv, "_inc_api_error", lambda endpoint, code: counted.append((endpoint, code)))

    res = types.SimpleNamespace(text="答案", llm_usage={"input_tokens": 10, "output_tokens": 5})

    async def run():
        return await srv._account_llm_cost_and_budget(
            endpoint="query",
            tenant="tenant-a",
            res=res,
            llm=None,
            model=_GPT_MINI,
            provider="openai",
            prompt="p",
            latency_s=None,
            trace=None,
            out=None,
            degrade_reasons=reasons,
        )

    payload = asyncio.run(run())

    assert payload is not None and payload["total_tokens"] == 15
    assert reasons == ["quota_backend_unavailable"]
    assert ("query", "quota_backend_unavailable") in counted


def test_auth_config_error_is_not_swallowed_by_quota_helper(monkeypatch):
    """`consume_tokens` 抛 AuthConfigError 时必须落到 degraded，而不是冒泡成 500 / 静默丢弃。"""
    limiter = _install_quota(monkeypatch, budget=100_000)

    async def _boom(tenant_id: str, tokens: int) -> int:
        raise AuthConfigError("quota_backend_error")

    monkeypatch.setattr(limiter, "consume_tokens", _boom)
    reasons: list[str] = []
    res = types.SimpleNamespace(text="答案", llm_usage={"input_tokens": 1, "output_tokens": 1})

    async def run():
        return await srv._account_llm_cost_and_budget(
            endpoint="stream",
            tenant="tenant-a",
            res=res,
            llm=None,
            model=_GPT_MINI,
            provider="openai",
            prompt="p",
            latency_s=None,
            trace=None,
            out=None,
            degrade_reasons=reasons,
        )

    assert asyncio.run(run()) is not None
    assert reasons == ["quota_backend_unavailable"]


# ---------------------------------------------------------------------------
# 5. _FakeLLM（无 key）→ 不扣预算，且响应里不出现 llm_cost
# ---------------------------------------------------------------------------


def test_synthetic_llm_does_not_charge_and_omits_llm_cost(api_env, monkeypatch):
    limiter = _install_quota(monkeypatch, budget=100_000)
    monkeypatch.setattr(_FakeSettings, "api_key", "")
    monkeypatch.setattr(_FakeSettings, "openai_api_key", "")
    # 合成路径下即便 LoopResult 带了 usage 也不许记账（防止把 placeholder 当真实用量）
    monkeypatch.setattr(_StubLoop, "usage", {"input_tokens": 999, "output_tokens": 999})

    async def run():
        with bind_principal(_principal("tenant-a")):
            result = await _call_query()
        return result, await limiter.usage("tenant-a")

    result, used = asyncio.run(run())
    body, _ = _as_dict(result)

    assert api_env.calls == 1
    assert "llm_cost" not in body, "合成路径不产生成本字段（本文件锁定的取舍）"
    assert body["degraded"] is True and "synthetic" in body["degraded_reason"]
    # 合成路径不得额外新增 degraded 原因（尤其不许把"usage 缺失"算到 _FakeLLM 头上）
    assert "llm_usage_missing" not in body["degraded_reason"]
    assert used == 0, "降级路径不得扣租户预算"


# ---------------------------------------------------------------------------
# 5b. 真实链路但后端没给 usage → 不记账/不估算，但必须显式降级（不许假成功）
# ---------------------------------------------------------------------------


def test_usage_missing_degrades_and_never_charges(api_env, monkeypatch):
    limiter = _install_quota(monkeypatch, budget=100_000)
    monkeypatch.setattr(_StubLoop, "usage", None)  # 后端静默：没有任何 usage_metadata
    counted: list[tuple[str, str]] = []
    monkeypatch.setattr(srv, "_inc_api_error", lambda endpoint, code: counted.append((endpoint, code)))
    charges: list[tuple[str, int]] = []

    async def _record_consume(tenant_id: str, tokens: int) -> int:
        charges.append((tenant_id, int(tokens)))
        return 0

    monkeypatch.setattr(limiter, "consume_tokens", _record_consume)

    async def run():
        with bind_principal(_principal("tenant-a")):
            return await _call_query()

    result = asyncio.run(run())
    body, headers = _as_dict(result)

    assert body["text"] == api_env.text, "答案照发（不能因为没计量就丢结果）"
    assert body["degraded"] is True
    assert "llm_usage_missing" in body["degraded_reason"]
    assert headers.get("x-degraded") == "true"
    assert "llm_cost" not in body, "没有可信计量就不产成本字段（更不许估算冒充）"
    assert charges == [], "没有后端 usage 就不扣预算（无计量不扣，但要显式降级）"
    assert ("query", "llm_usage_missing") in counted


def test_stream_usage_missing_emits_degraded_without_cost(api_env, monkeypatch, tmp_path):
    limiter = _install_quota(monkeypatch, budget=100_000)
    monkeypatch.setattr(_StubLoop, "usage", None)
    charges: list[tuple[str, int]] = []

    async def _record_consume(tenant_id: str, tokens: int) -> int:
        charges.append((tenant_id, int(tokens)))
        return 0

    monkeypatch.setattr(limiter, "consume_tokens", _record_consume)

    async def run():
        with bind_principal(_principal("tenant-a")):
            return await _call_stream(trace_dir=str(tmp_path / "missing_usage"))

    events = _sse_events(asyncio.run(run()))

    degraded = [e for e in events if e.get("type") == "degraded"]
    assert degraded, "真实链路没拿到 usage 必须发 degraded 事件"
    assert "llm_usage_missing" in degraded[0]["reason"]
    assert not [e for e in events if e.get("type") == "llm_cost"], "无计量不出 llm_cost 事件"
    assert charges == []
    assert any("delta" in e for e in events), "正文仍要发出去"


def test_usage_missing_helper_degrades_and_skips_consume(monkeypatch):
    """helper 单元口径：synthetic=False + usage=None → 只降级，不扣费、不返回 payload。"""
    limiter = _install_quota(monkeypatch, budget=100_000)
    reasons: list[str] = []
    counted: list[tuple[str, str]] = []
    monkeypatch.setattr(srv, "_inc_api_error", lambda endpoint, code: counted.append((endpoint, code)))
    charges: list[tuple[str, int]] = []

    async def _record_consume(tenant_id: str, tokens: int) -> int:
        charges.append((tenant_id, int(tokens)))
        return 0

    monkeypatch.setattr(limiter, "consume_tokens", _record_consume)
    res = types.SimpleNamespace(text="答案", llm_usage=None)

    async def run():
        return await srv._account_llm_cost_and_budget(
            endpoint="query",
            tenant="tenant-a",
            res=res,
            llm=None,
            model=_GPT_MINI,
            provider="openai",
            prompt="p",
            latency_s=None,
            trace=None,
            out=None,
            degrade_reasons=reasons,
        )

    assert asyncio.run(run()) is None
    assert reasons == ["llm_usage_missing"]
    assert counted == [("query", "llm_usage_missing")]
    assert charges == []


# ---------------------------------------------------------------------------
# 6. 目录外模型 → cost_usd=None（不猜价、不算 0）
# ---------------------------------------------------------------------------


def test_unpriced_model_reports_none_cost(api_env, monkeypatch, tmp_path):
    limiter = _install_quota(monkeypatch, budget=100_000)
    monkeypatch.setattr(_StubFactory, "model", _UNPRICED)
    trace_dir = tmp_path / "unpriced"

    async def run():
        with bind_principal(_principal("tenant-a")):
            result = await _call_query(trace_dir=str(trace_dir))
        return result, await limiter.usage("tenant-a")

    result, used = asyncio.run(run())
    body, _ = _as_dict(result)
    cost = body["llm_cost"]

    assert cost["model"] == _UNPRICED
    assert cost["cost_usd"] is None, "目录外模型不许猜价，更不许用 0 冒充免费"
    assert cost["priced"] is False
    trace_costs = [r for r in _read_trace(trace_dir) if r.get("type") == "llm_cost"]
    assert trace_costs and trace_costs[0]["cost_usd"] is None
    assert used == 1500, "没定价也要按 token 记账（预算与价格无关）"


# ---------------------------------------------------------------------------
# 7. LoopResult.llm_usage 只透出后端真实累计
# ---------------------------------------------------------------------------


class _UsageLLM:
    """流式替身：带 usage_metadata 的 chunk（模拟后端计量）。"""

    def stream_chat(self, goal, timeout=None):  # noqa: ARG002
        yield {"type": "text", "text": "答案", "usage_metadata": {"input_tokens": 12, "output_tokens": 7}}


class _SilentLLM:
    """流式替身：后端完全不报 usage。"""

    def stream_chat(self, goal, timeout=None):  # noqa: ARG002
        yield {"type": "text", "text": "答案"}


def test_loop_result_carries_backend_usage():
    result = AgentLoop(llm=_UsageLLM(), max_iterations=1).run("goal")

    assert result.llm_usage == {"input_tokens": 12, "output_tokens": 7}


def test_loop_result_usage_is_none_when_backend_silent():
    result = AgentLoop(llm=_SilentLLM(), max_iterations=1).run("goal")

    assert result.llm_usage is None, "后端没给 usage 就保持 None，不用字符估算凑数"

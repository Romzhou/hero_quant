"""P0-4：DedupStore 接线（tools.registry.execute_tool_call）行为锁定。

背景：governance.dedup.DedupStore 已实现 PENDING→SUCCESS/FAILED 状态机，但
agent/loop.py 与 tools/registry.py 零引用，等于幂等键机制未接线（L1 而非 L2）。
本文件锁定执行器的三条不变量：
1. dedup_store=None 时行为与 spec.func(**args) 完全一致（默认零影响）；
2. 同一 idempotency_key 的副作用在**跨实例（worker 崩溃重启）**后仍只发生一次；
3. 未成功 claim（PENDING 冲突超时 / 既有 FAILED）时绝不执行任何副作用（fail-closed）。

测试用 tmp_path 下的 SQLite 文件构造 DedupStore（memory:// 仅单测可用且不跨实例，
无法证明"重启后仍去重"，故禁用）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from hero_quant.auth.context import bind_principal
from hero_quant.auth.principal import Principal
from hero_quant.governance.dedup import DedupStore
from hero_quant.tools.registry import ToolSpec, execute_tool_call

KEY = "tenantA:wf1:step1:t_counter:biz1"


class _CounterTool:
    """带副作用的探针：每次真正执行 self.calls 自增。"""

    def __init__(self) -> None:
        self.calls = 0
        self.seen: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        self.seen.append(dict(kwargs))
        return {"calls": self.calls, "echo": dict(kwargs)}


def _spec(func: Any, name: str = "t_counter") -> ToolSpec:
    return ToolSpec(name=name, description="dedup wiring probe", func=func)


def _store(db_path: Any) -> DedupStore:
    """每个实例模拟一个独立 worker 进程（真实 SQLite 文件，非 memory://）。"""
    return DedupStore(db_path=str(db_path))


def _keys(db_path: Any) -> list[str]:
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute("SELECT idempotency_key FROM tool_call_dedup").fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# 1. 默认零影响
# ---------------------------------------------------------------------------


def test_without_store_runs_every_time() -> None:
    """无 dedup_store：不得去重、不得改返回值形态。"""
    counter = _CounterTool()
    spec = _spec(counter)

    first = execute_tool_call(spec, {"symbol": "600519.SH"})
    second = execute_tool_call(spec, {"symbol": "600519.SH"})

    assert counter.calls == 2
    assert first == {"calls": 1, "echo": {"symbol": "600519.SH"}}
    assert second == {"calls": 2, "echo": {"symbol": "600519.SH"}}


def test_without_store_ignores_idempotency_key() -> None:
    """显式给 key 但没给 store：仍视为无去重（避免"看起来接上了"的静默假接）。"""
    counter = _CounterTool()
    spec = _spec(counter)

    execute_tool_call(spec, {}, idempotency_key=KEY)
    execute_tool_call(spec, {}, idempotency_key=KEY)

    assert counter.calls == 2


# ---------------------------------------------------------------------------
# 2. 同一 key 跨实例只执行一次（worker 崩溃重启）
# ---------------------------------------------------------------------------


def test_same_key_runs_once_across_worker_restart(tmp_path: Any) -> None:
    db = tmp_path / "dedup.db"
    counter = _CounterTool()
    spec = _spec(counter)

    first = execute_tool_call(spec, {"symbol": "600519.SH"}, dedup_store=_store(db), idempotency_key=KEY)
    # 模拟 worker 崩溃重启：全新的 DedupStore 实例，进程内缓存为空，只能靠 SQLite
    second = execute_tool_call(spec, {"symbol": "600519.SH"}, dedup_store=_store(db), idempotency_key=KEY)

    assert counter.calls == 1, "同一幂等键在重启后不得重复执行副作用"
    assert first == {"calls": 1, "echo": {"symbol": "600519.SH"}}
    assert second == first, "重放必须返回首次结果，不得返回空值/None"


def test_success_record_persisted_in_sqlite(tmp_path: Any) -> None:
    db = tmp_path / "dedup.db"
    counter = _CounterTool()

    execute_tool_call(_spec(counter), {"symbol": "000001.SZ"}, dedup_store=_store(db), idempotency_key=KEY)

    rec = _store(db).get(KEY)
    assert rec is not None and rec["status"] == "SUCCESS"


# ---------------------------------------------------------------------------
# 3. 反向断言：换 key 会重新执行（不是全局结果缓存）
# ---------------------------------------------------------------------------


def test_different_key_runs_again(tmp_path: Any) -> None:
    db = tmp_path / "dedup.db"
    store = _store(db)
    counter = _CounterTool()
    spec = _spec(counter)
    other_key = "tenantA:wf1:step1:t_counter:biz2"

    first = execute_tool_call(spec, {"symbol": "600519.SH"}, dedup_store=store, idempotency_key=KEY)
    second = execute_tool_call(spec, {"symbol": "600519.SH"}, dedup_store=store, idempotency_key=other_key)

    assert counter.calls == 2, "换 key 必须重新执行，否则就是全局缓存而非幂等"
    assert first == {"calls": 1, "echo": {"symbol": "600519.SH"}}
    assert second == {"calls": 2, "echo": {"symbol": "600519.SH"}}


def test_non_string_result_types_round_trip(tmp_path: Any) -> None:
    """结果类型必须精确回放（"123" 不得变成 123，dict/list 不得变成 str）。"""
    db = tmp_path / "dedup.db"
    payload = {"ok": True, "rows": [1, 2, 3], "label": "123"}

    execute_tool_call(_spec(lambda **_: payload), {}, dedup_store=_store(db), idempotency_key=KEY)
    replay = execute_tool_call(_spec(lambda **_: payload), {}, dedup_store=_store(db), idempotency_key=KEY)

    assert replay == payload
    assert isinstance(replay["label"], str)


# ---------------------------------------------------------------------------
# 4. FAILED 记录后不得再执行副作用，且异常原样抛出
# ---------------------------------------------------------------------------


def test_tool_exception_propagates_and_marks_failed(tmp_path: Any) -> None:
    db = tmp_path / "dedup.db"
    store = _store(db)
    calls: list[int] = []

    def boom(**_: Any) -> Any:
        calls.append(1)
        raise ValueError("boom-detail")

    with pytest.raises(ValueError, match="boom-detail"):
        execute_tool_call(_spec(boom), {}, dedup_store=store, idempotency_key=KEY)

    rec = store.get(KEY)
    assert rec is not None and rec["status"] == "FAILED"
    assert "ValueError" in str(rec.get("error"))
    assert len(calls) == 1


def test_failed_record_blocks_reexecution(tmp_path: Any) -> None:
    db = tmp_path / "dedup.db"
    calls: list[int] = []

    def boom(**_: Any) -> Any:
        calls.append(1)
        raise ValueError("boom-detail")

    with pytest.raises(ValueError):
        execute_tool_call(_spec(boom), {}, dedup_store=_store(db), idempotency_key=KEY)

    # 另一个 worker 拿到同一 key：必须抛稳定 RuntimeError，且绝不重跑副作用
    with pytest.raises(RuntimeError) as excinfo:
        execute_tool_call(_spec(boom), {}, dedup_store=_store(db), idempotency_key=KEY)

    assert "FAILED" in str(excinfo.value).upper()
    assert len(calls) == 1, "既有 FAILED 记录不得触发第二次副作用"


# ---------------------------------------------------------------------------
# 5. PENDING 冲突超时：fail-closed，无副作用
# ---------------------------------------------------------------------------


def test_pending_timeout_fails_closed_without_side_effect(tmp_path: Any) -> None:
    db = tmp_path / "dedup.db"
    holding = _store(db)
    assert holding.insert_pending(KEY, "t_counter") is True  # 另一 worker 占位后挂住

    counter = _CounterTool()
    with pytest.raises(RuntimeError) as excinfo:
        execute_tool_call(_spec(counter), {"symbol": "600519.SH"}, dedup_store=_store(db),
                          idempotency_key=KEY, wait_timeout=0.05)

    assert "PENDING" in str(excinfo.value).upper()
    assert counter.calls == 0, "未成功 claim 时不得执行任何副作用"


# ---------------------------------------------------------------------------
# 6. 无显式 key 时的派生规则（租户/工作流/步骤/业务）
# ---------------------------------------------------------------------------


def test_derived_key_uses_bound_tenant(tmp_path: Any) -> None:
    db = tmp_path / "dedup.db"
    counter = _CounterTool()
    spec = _spec(counter)

    with bind_principal(Principal(subject="u1", tenant_id="tenantA")):
        first = execute_tool_call(spec, {"symbol": "600519.SH"}, dedup_store=_store(db),
                                  workflow_id="wf1", step_id="step1", business_id="biz1")
    with bind_principal(Principal(subject="u1", tenant_id="tenantA")):
        second = execute_tool_call(spec, {"symbol": "600519.SH"}, dedup_store=_store(db),
                                   workflow_id="wf1", step_id="step1", business_id="biz1")

    assert counter.calls == 1
    assert second == first
    assert _keys(db) == [KEY], "派生键必须精确等于 tenant:wf:step:tool:biz"


def test_derived_key_falls_back_to_default_tenant_offline(tmp_path: Any) -> None:
    """离线/无租户上下文：回落 default，仍需稳定去重（不得抛 MissingTenant）。"""
    db = tmp_path / "dedup.db"
    counter = _CounterTool()
    spec = _spec(counter)

    first = execute_tool_call(spec, {"symbol": "600519.SH"}, dedup_store=_store(db), workflow_id="wf1", step_id="step1")
    second = execute_tool_call(spec, {"symbol": "600519.SH"}, dedup_store=_store(db), workflow_id="wf1", step_id="step1")

    assert counter.calls == 1
    assert second == first == {"calls": 1, "echo": {"symbol": "600519.SH"}}
    keys = _keys(db)
    assert len(keys) == 1 and keys[0].startswith("default:wf1:step1:t_counter:")


def test_derived_key_sanitizes_colons(tmp_path: Any) -> None:
    """入参含冒号不得让 derive_key 抛 ValueError（清洗而非崩溃）。"""
    db = tmp_path / "dedup.db"
    counter = _CounterTool()
    spec = _spec(counter)

    execute_tool_call(spec, {}, dedup_store=_store(db), workflow_id="wf:1", step_id="s:1", business_id="b:1")
    second = execute_tool_call(spec, {}, dedup_store=_store(db), workflow_id="wf:1", step_id="s:1", business_id="b:1")

    assert counter.calls == 1
    assert second == {"calls": 1, "echo": {}}
    keys = _keys(db)
    assert len(keys) == 1
    assert len(keys[0].split(":")) == 5, "清洗后仍必须是 5 段键（derive_key 契约）"


# ---------------------------------------------------------------------------
# 7. 不可序列化结果：不算失败、不返回空值
# ---------------------------------------------------------------------------


def test_non_serializable_result_returned_as_is(tmp_path: Any, caplog: Any) -> None:
    db = tmp_path / "dedup.db"
    sentinel = object()

    with caplog.at_level("WARNING"):
        first = execute_tool_call(_spec(lambda **_: sentinel), {}, dedup_store=_store(db), idempotency_key=KEY)

    assert first is sentinel, "不可序列化也必须返回真实结果，不得伪装失败/空值"
    assert any("not JSON serializable" in r.getMessage() for r in caplog.records), "必须留下告警而非静默"

    # 重放：返回带 degraded 标记的描述符，明确告知"结果未持久化"，绝不返回 None 冒充成功
    replay = execute_tool_call(_spec(lambda **_: sentinel), {}, dedup_store=_store(db), idempotency_key=KEY)
    assert isinstance(replay, dict) and replay.get("degraded") is True
    assert replay.get("reason") == "result_not_json_serializable"

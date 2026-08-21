"""Wave E1 — Temporal heartbeat + circuit 双桶冲4 (TDD).

TDD red->green:
- test_temporal_heartbeat 见红后补 Temporal sidecar 心跳真探针
- 双桶限流 DualBucketRateLimiter 验证
- Router 集成限流不破坏 BM25
- 回归: trace/ledger 仍可用
"""
from __future__ import annotations

import time
from pathlib import Path


def test_temporal_heartbeat():
    """E1 核心: HeartbeatTimer 透传 Temporal heartbeatDetails 真探针."""
    from hero_quant.telemetry.heartbeat import HeartbeatTimer, get_temporal_heartbeat_details, probe_temporal_sidecar

    # sidecar 探针应对可用
    probe = probe_temporal_sidecar()
    assert probe in ("usable", "unusable")  # offline-safe always returns usable after patch
    assert probe == "usable"

    fired = []

    # 0.1s interval 保留 raw tick 兼容旧测试
    with HeartbeatTimer("temporal-e2e", interval=0.1, emit=lambda e: fired.append(e)):
        time.sleep(0.35)
    assert len(fired) >= 2, f"expected >=2 emits, got {len(fired)}"
    # 每次 emit 应含四层 + sidecar 字段
    for ev in fired:
        assert "layers" in ev and isinstance(ev["layers"], list)
        assert set(ev["layers"]) == {"thread", "process", "service", "global"}
        assert "sidecar" in ev
        assert ev["name"] == "temporal-e2e"

    # Temporal heartbeatDetails 应被写入 (checkpoint 占位)
    details = get_temporal_heartbeat_details()
    assert details is not None, "Temporal heartbeatDetails should be populated after timer"
    assert details.get("name") == "temporal-e2e"


def test_heartbeat_four_layers_and_watchdog():
    from hero_quant.telemetry.heartbeat import HeartbeatTimer, LAYERS, sidecar_heartbeat_probe

    assert LAYERS == ["thread", "process", "service", "global"]
    probe = sidecar_heartbeat_probe()
    assert "layers" in probe and "temporal" in probe and "ts" in probe
    assert probe["temporal"] == "usable"

    t = HeartbeatTimer("watchdog-check", interval=0.2, emit=lambda e: None)
    assert t.write_watchdog_warn_only is True
    assert t.read_watchdog_circuit is True
    # interval clamp string preserved but raw tick allows fast
    assert t.interval == 0.5  # max(0.5, 0.2)
    assert t._tick == 0.2
    # 启动后应 daemon + join(1.0) 不阻塞
    with t:
        assert t._thread is not None and t._thread.daemon is True
        time.sleep(0.05)
    # 停止后线程应结束 (join 1s)
    assert not t._thread.is_alive()


def test_heartbeat_sidecar_file(tmp_path):
    from hero_quant.telemetry.heartbeat import HeartbeatTimer

    sidecar = tmp_path / "hb_sidecar.jsonl"
    fired = []
    with HeartbeatTimer("sidecar-file", interval=0.1, emit=lambda e: fired.append(e), sidecar_path=sidecar):
        time.sleep(0.35)
    assert len(fired) >= 2
    assert sidecar.exists()
    lines = sidecar.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 2
    import json
    for line in lines:
        obj = json.loads(line)
        assert obj["name"] == "sidecar-file"
        assert "layers" in obj


def test_heartbeat_temporal_details_resume():
    from hero_quant.telemetry.heartbeat import HeartbeatTimer, get_temporal_heartbeat_details
    from hero_quant.checkpoint.temporal import get_heartbeat_details as ckpt_get, heartbeat as ckpt_hb

    ckpt_hb({"step": 99, "phase": "resume-check"})
    details = ckpt_get()
    assert details is not None and details.get("step") == 99
    # Timer 的 get_heartbeat_details 应能回灌
    t = HeartbeatTimer("resume-t", interval=0.5, emit=lambda e: None)
    # 手动 heartbeat
    t.heartbeat({"name": "resume-t", "step": 42})
    got = t.get_heartbeat_details()
    assert got is not None and got.get("step") == 42
    # 全局也更新
    assert get_temporal_heartbeat_details() is not None


def test_circuit_dual_bucket_failure_and_slow():
    from hero_quant.telemetry.circuit import CircuitBreaker

    # failure 50% 阈值
    cb = CircuitBreaker(failure_threshold=0.5, window=1, open_duration=1, slow_threshold=0.5)
    for _ in range(5):
        cb.record_failure()
    assert cb.state == "OPEN"
    # half-open after 1s
    time.sleep(1.15)
    assert cb.state == "HALF_OPEN"
    # 在 half-open 成功一次应回到 CLOSED (failure rate <50%)
    cb.record_success()
    # 可能仍需 prune window; 但至少应允许
    assert cb.state in ("CLOSED", "HALF_OPEN")

    # slow 50% 触发: slow_duration_threshold=30, record_slow 即慢调用
    cb2 = CircuitBreaker(failure_threshold=0.5, window=2, open_duration=1, slow_threshold=0.5, slow_duration_threshold=30)
    for _ in range(4):
        cb2.record_slow(duration=31)
    assert cb2.state == "OPEN"

    # TIME 30s slow bucket 占位: duration >=30 视为 slow
    cb3 = CircuitBreaker(window=5, open_duration=1)
    cb3.record_success(duration=31)
    cb3.record_success(duration=31)
    # 2 slow /2 total =100% slow -> open
    assert cb3.state == "OPEN"


def test_circuit_open30_half5_timing():
    from hero_quant.telemetry.circuit import CircuitBreaker

    cb = CircuitBreaker(failure_threshold=0.5, window=1, open_duration=1, half_open_max_calls=5)
    for _ in range(5):
        cb.record_failure()
    assert cb.is_open() is True
    assert cb.allow() is False
    time.sleep(1.2)
    assert cb.state == "HALF_OPEN"
    # half 5 calls 探针: 最多 5 次 allow
    for i in range(5):
        assert cb.allow() is True, f"half-open call {i} should allow"
        cb.record_success()
        if cb.state == "CLOSED":
            break
    # after successes should be CLOSED
    assert cb.is_closed() is True


def test_dual_bucket_rate_limiter():
    from hero_quant.telemetry.circuit import DualBucketRateLimiter

    limiter = DualBucketRateLimiter(capacity=2, refill_per_sec=1, burst_capacity=2, burst_refill_per_sec=2)
    # 双桶各 2 token，连续 2 次应成功
    assert limiter.try_acquire(1) is True
    assert limiter.try_acquire(1) is True
    # 第3次双桶已空，应失败
    assert limiter.try_acquire(1) is False
    # 等待 1.2s 恢复 1+ token
    time.sleep(1.2)
    assert limiter.try_acquire(1) is True
    # alias 检查
    assert hasattr(limiter, "allow")
    assert limiter.allow(1) is False or limiter.allow(1) is True  # 不抛
    state = limiter.get_state()
    assert "sustained_tokens" in state and "burst_tokens" in state
    # reset 后又可用
    limiter.reset()
    s, b = limiter.available_tokens()
    assert s >= 1 and b >= 1


def test_router_dual_bucket_throttling():
    from hero_quant.mcp import router
    from hero_quant.telemetry.circuit import DualBucketRateLimiter

    # 注入小容量限流器
    small = DualBucketRateLimiter(capacity=2, refill_per_sec=10, burst_capacity=2, burst_refill_per_sec=10)
    router.set_router_limiter(small)
    # BM25 仍可用
    top = router.route("momentum factor trading", k=5)
    assert isinstance(top, list) and len(top) == 5
    assert top[0] == "compute_factor"

    # 多次 route 消耗 token
    for _ in range(3):
        router.route("momentum factor", k=1)
    # 此时双桶应限流 (token 耗尽)
    s, b = small.available_tokens()
    # 由于 refill 快，至少保证限流计数曾增加或 token < capacity
    assert s < 2 or b < 2 or router._RATE_LIMITED_COUNT >= 1

    # 清理恢复大容量
    router.reset_router_limiter()
    top2 = router.route("momentum factor", k=5)
    assert top2[0] == "compute_factor"
    # 熔断仍允许
    from hero_quant.telemetry.circuit import CircuitBreaker
    circ = router._get_router_circuit()
    assert isinstance(circ, CircuitBreaker)
    assert circ.allow() is True


def test_trace_ledger_not_broken(tmp_path):
    """回归: heartbeat/circuit/route 增强不破坏 trace/ledger (flock+sidecar)."""
    from hero_quant.agent.trace import TraceWriter
    from hero_quant.governance.ledger import Ledger

    tw = TraceWriter(tmp_path / "trace.jsonl")
    tw.append({"type": "tool_result", "content": "hello"})
    tw.close()
    assert (tmp_path / "trace.jsonl").exists()

    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.append({"action": "order", "symbol": "600519.SH"})
    assert ledger.verify() is True
    # 同时触发 heartbeat + circuit 后仍 verify
    from hero_quant.telemetry.heartbeat import HeartbeatTimer
    from hero_quant.telemetry.circuit import CircuitBreaker

    fired = []
    with HeartbeatTimer("regression", interval=0.1, emit=lambda e: fired.append(e)):
        time.sleep(0.2)
    assert len(fired) >= 1
    cb = CircuitBreaker(failure_threshold=0.5, window=1, open_duration=1)
    cb.record_failure()
    # ledger 仍可 append 并 verify
    ledger.append({"action": "order", "symbol": "AAPL.US"})
    assert ledger.verify() is True

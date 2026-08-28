"""Task17 TDD: graph 调度深度与熔断锁 (scan_core_muse.log:495-549)."""
from pathlib import Path


def test_delegation_depth_increments():
    """plan_node 与 _plan 必须返回 delegation_depth: depth+1 且 Send 载荷 depth+1."""
    src = Path("src/hero_quant/agent/graph.py").read_text(encoding="utf-8")
    # 必须存在 depth + 1 的递增（至少两处：plan_node 与 _plan）
    assert "delegation_depth" in src
    # 源码断言：存在 delegation_depth: depth + 1
    assert "depth + 1" in src or "depth+1" in src, "missing depth+1 increment"
    # 分别检查 plan_node 与 _plan 返回处有 depth+1；用计数近似
    cnt = src.count("depth + 1") + src.count("depth+1")
    assert cnt >= 3, f"expected >=3 depth+1 occurrences (plan_node+_plan+Send), got {cnt}: {src.count('depth + 1')}"
    # Send 载荷必须含 depth+1
    assert '"delegation_depth": depth + 1' in src or '"delegation_depth":depth+1' in src or "'delegation_depth': depth + 1" in src or "delegation_depth\": depth + 1" in src, "Send payload missing depth+1"
    # Send 构造需传递带 depth+1 的 state 副本（**state 展开）
    assert "**state" in src and "delegation_depth" in src, "Send should carry {**state, \"delegation_depth\": depth+1}"


def test_breaker_threadsafe():
    """_breaker 调用处必须有 threading.Lock 保护或 check_and_add 原子."""
    src = Path("src/hero_quant/agent/graph.py").read_text(encoding="utf-8")
    has_lock = "threading.Lock" in src or "threading.RLock" in src or "_breaker_lock" in src
    assert has_lock, "missing threading.Lock / _breaker_lock for _breaker"
    # 调用处被锁保护：with _breaker_lock
    has_with = "with _breaker_lock" in src or "with threading" in src
    has_atomic = "check_and_add" in src
    assert has_with or has_atomic, "breaker call must be under Lock (with _breaker_lock) or use check_and_add atomically"
    # 顶部 import 窄化：不应有裸 except Exception 吞 ImportError
    # 至少存在 except ImportError 且有 warning
    assert "except ImportError" in src, "top imports should narrow to except ImportError"
    assert "warning" in src.lower(), "import fallback should log warning"

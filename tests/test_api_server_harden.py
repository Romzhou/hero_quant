"""Task14 TDD — server guards, SSE async, path hardening."""

import pathlib
import inspect


def test_request_counter_none_guarded():
    """源码断言 REQUEST_COUNTER 调用处有 None guard 或 try."""
    src = pathlib.Path("src/hero_quant/api/server.py").read_text(encoding="utf-8")
    # 检查 live/ready/query 路径的 REQUEST_COUNTER 调用是否有 None guard
    # 最小要求：源码中存在对 REQUEST_COUNTER 的 None 判断或受 try 包裹
    has_none_guard = "if REQUEST_COUNTER" in src or "REQUEST_COUNTER is not None" in src
    has_try_guard = src.count("REQUEST_COUNTER.labels") >= 1 and "try:" in src
    # 要求至少有显式 None guard（task 要求加 None guard）
    assert has_none_guard, "REQUEST_COUNTER calls must have None guard (if REQUEST_COUNTER is not None)"
    # 且 live/ready 不应裸调；检查 live 函数体内有 guard
    # 简单校验：live 定义后的 5 行内应有 guard
    live_idx = src.find("def live")
    ready_idx = src.find("def ready")
    # 取 live..ready 区间
    segment = src[live_idx : ready_idx] if live_idx != -1 and ready_idx != -1 else src
    assert "if REQUEST_COUNTER" in segment or "REQUEST_COUNTER is not None" in segment, "live() must guard REQUEST_COUNTER"


def test_negative_offset_rejected():
    """offset<0 → HTTPException 400 (via TestClient or direct call)."""
    from fastapi.testclient import TestClient
    from fastapi import HTTPException
    from hero_quant.api.server import app, trace_events

    # 方式1：TestClient
    client = TestClient(app)
    r = client.get("/v1/trace/events", params={"offset": -1})
    if r.status_code == 400:
        return
    # 方式2：直接调用函数应抛 HTTPException 400
    import inspect as _inspect

    # 构造最小 Request
    class _FakeReq:
        headers = {"accept": "application/json"}

    try:
        # trace_events 可能为同步函数，签名 (request, offset)
        if _inspect.iscoroutinefunction(trace_events):
            import asyncio

            asyncio.run(trace_events(_FakeReq(), offset=-1))  # type: ignore
        else:
            trace_events(_FakeReq(), offset=-1)  # type: ignore
        assert False, "trace_events should raise HTTPException 400 for offset<0"
    except HTTPException as e:
        assert e.status_code == 400
    except AssertionError:
        raise
    except Exception as e:
        # 若抛的是 HTTPException 包装后的 400 response，也算通过
        assert False, f"unexpected exception {e!r}"


def test_dist_traversal_blocked():
    """源码断言 _dist_path 处理用 resolve+is_relative_to."""
    src = pathlib.Path("src/hero_quant/api/server.py").read_text(encoding="utf-8")
    # 需在 serve_spa / candidate 处理处出现 resolve() 且 is_relative_to
    assert "resolve()" in src, "must use Path.resolve() for dist traversal check"
    assert "is_relative_to" in src, "must use is_relative_to for dist traversal check"
    # 且 candidate 与 _dist_path / full_path 关联
    # 检查 candidate 定义附近有 resolve+is_relative_to
    idx = src.find("candidate")
    # 找最近的 candidate 赋值
    cand_idx = src.find("candidate =")
    if cand_idx == -1:
        cand_idx = src.find("candidate=")
    assert cand_idx != -1, "candidate path handling not found"
    window = src[cand_idx : cand_idx + 800]
    assert "resolve()" in window and "is_relative_to" in window, "candidate handling must resolve+is_relative_to"

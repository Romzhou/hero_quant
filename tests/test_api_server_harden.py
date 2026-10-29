"""Task14 TDD — server guards, SSE async, path hardening."""



def test_request_counter_none_guarded(monkeypatch):
    """REQUEST_COUNTER 未初始化（None）时健康检查与回测端点不得崩。"""
    import hero_quant.api.server as srv
    from fastapi.testclient import TestClient

    monkeypatch.setattr(srv, "REQUEST_COUNTER", None, raising=False)
    monkeypatch.setattr(srv, "_get_backtest_bundle", lambda: {"metrics": {}}, raising=False)
    c = TestClient(srv.app)
    assert c.get("/live").status_code == 200
    assert c.get("/v1/backtest/metrics.json").status_code == 200


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
    """编码后的 ../ 不得越出 dist，更不能把仓库文件当静态资源返回。"""
    from fastapi.testclient import TestClient
    from hero_quant.api.server import app

    c = TestClient(app)
    for path in ("/%2e%2e/pyproject.toml", "/..%2Fpyproject.toml", "/%2e%2e%2fpyproject.toml", "/../pyproject.toml"):
        r = c.get(path, headers={"accept": "text/plain"})
        assert r.status_code == 404, f"{path} 未被拒绝: {r.status_code} {r.text[:120]}"
        assert "[project]" not in r.text and "hero-quant" not in r.text

"""Wave F frontend SPA + E2E hardening TDD red."""
from fastapi.testclient import TestClient


def test_spa_routes_serve_html():
    """SPA 路由二态断言：dist 可用 → 200 HTML；dist/index 缺失 → 503 fail-closed。

    中文：CI 干净 checkout 没有 frontend/dist，D 线已把「200 占位页」改成 503；
    不能用固定 200 或 status_code in (200,503) 的二选一（后者恒真），必须按服务端
    实际挂载分支断言：_dist_path is None → X-Frontend: missing；
    dist 在但 index.html 缺失 → X-Frontend: index-missing。
    """
    import pathlib

    import hero_quant.api.server as srv
    from hero_quant.api.server import app

    c = TestClient(app)
    dist = getattr(srv, "_dist_path", None)
    index_ok = bool(dist) and (pathlib.Path(dist) / "index.html").is_file()
    routes = ["/", "/dashboard", "/research", "/backtest", "/risk", "/settings", "/chat"]
    for path in routes:
        r = c.get(path, headers={"Accept": "text/html"})
        if index_ok:
            assert r.status_code == 200, f"{path} got {r.status_code} {r.text[:200]}"
            txt = r.text.lower()
            assert "<div id=\"root\"" in txt or "<!doctype html" in txt, f"{path} not html: {r.text[:200]}"
            assert "hero" in txt or "vite" in txt or "量化" in txt or "root" in txt
        else:
            expected = "missing" if dist is None else "index-missing"
            assert r.status_code == 503, f"{path} 缺 dist/index 必须 503，got {r.status_code}"
            assert r.headers.get("X-Frontend") == expected, (
                f"{path} X-Frontend 应为 {expected}，got {r.headers.get('X-Frontend')}"
            )
            assert r.headers.get("Retry-After") == "30"
            assert "503" in r.text, f"{path} 503 说明页缺失：{r.text[:200]}"
    if not index_ok:
        # JSON 探针必须看到 degraded + frontend 原因，不能假装 ok
        expected = "missing" if dist is None else "index-missing"
        j = c.get("/", headers={"Accept": "application/json"})
        assert j.status_code == 503
        body = j.json()
        assert body.get("status") == "degraded"
        assert body.get("frontend") == expected
        assert j.headers.get("X-Frontend") == expected
    # 缺失的静态资源始终 404（资源语义，不返回 index/503 说明页）
    static = c.get("/assets/definitely-missing.js")
    assert static.status_code == 404, f"missing .js must be 404, got {static.status_code}"


def test_health_and_metrics_and_wall_time():
    from hero_quant.api.server import app

    c = TestClient(app)
    # /live health JSON when not requesting html
    r = c.get("/live")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    # /ready：容忍本地 PG 缺席（无 PG 时 503 + memory 回退为预期语义）
    ready = c.get("/ready")
    assert ready.status_code in (200, 503)
    body = ready.json()
    if ready.status_code == 503:
        assert body.get("status") == "degraded"
        # 中文：降级时 checkpoint 标签可能是 memory（未配置 PG）或 pg（已配置但探活失败）——均为诚实降级
        assert body.get("checkpoint") in ("memory", "pg")
    else:
        assert body.get("status") == "ok"
    # /metrics contains wall_time（T4-1 需 X-Ticket 鉴权；匿名 401 为预期）
    from hero_quant.api import security as _sec_front_spa

    m_anon = c.get("/metrics")
    assert m_anon.status_code == 401
    m = c.get("/metrics", headers={"X-Ticket": _sec_front_spa.issue_ticket(ttl=60)})
    assert m.status_code == 200
    txt = m.text
    assert "wall_time" in txt.lower() or "wall-time" in txt.lower(), f"wall_time missing in metrics: {txt[:500]}"
    # also http_request_duration histogram
    assert "http_request_duration_seconds" in txt


def test_backtest_artifacts_and_trace_events():
    from hero_quant.api.server import app

    c = TestClient(app)
    # backtest artifacts for Research page
    for p, expect in [
        ("/v1/backtest/metrics.json", "sharpe"),
        ("/v1/backtest/positions.csv", "date"),
        ("/v1/backtest/tearsheet.html", "Tearsheet"),
    ]:
        r = c.get(p)
        assert r.status_code == 200, f"{p} {r.status_code} {r.text[:200]}"
        assert expect.lower() in r.text.lower(), f"{p} missing {expect}: {r.text[:200]}"
    # drawdowns.json：Research 页回撤 TopN（裸数组；depth 为百分比，与前端 Drawdown 类型对齐）。
    # 注意：无 pandas 时 bundle 走静态兜底（2 行单调 CSV，无回撤），故只断状态码+裸数组+条目形状，不强制非空。
    r = c.get("/v1/backtest/drawdowns.json")
    assert r.status_code == 200, f"/v1/backtest/drawdowns.json {r.status_code} {r.text[:200]}"
    data = r.json()
    assert isinstance(data, list), f"drawdowns must be a bare list, got {type(data)}"
    for item in data:
        assert isinstance(item.get("start"), str) and isinstance(item.get("end"), str)
        assert isinstance(item.get("depth"), (int, float)) and isinstance(item.get("duration"), (int, float))
    # trace events SSE or JSON
    r = c.get("/v1/trace/events?offset=0", headers={"Accept": "text/event-stream"})
    assert r.status_code == 200
    # should be event-stream or json
    ct = r.headers.get("content-type", "")
    assert "event-stream" in ct or "json" in ct or "text" in ct


def test_frontend_dist_reused():
    import pathlib

    import pytest

    dist = pathlib.Path("frontend/dist")
    if not dist.is_dir():
        pytest.skip("no dist in test job")
    assert (dist / "index.html").is_file(), "frontend/dist/index.html missing"
    # at least assets
    assets = list((dist / "assets").glob("*.js")) if (dist / "assets").exists() else []
    assert len(assets) >= 1, "frontend/dist/assets missing js"

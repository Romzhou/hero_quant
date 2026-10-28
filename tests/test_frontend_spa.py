"""Wave F frontend SPA + E2E hardening TDD red."""
import importlib
import os
import pathlib

import pytest
from fastapi.testclient import TestClient

FRONTEND_ROUTES = ["/", "/dashboard", "/research", "/backtest", "/risk", "/settings", "/chat"]
INDEX_SENTINEL = "hero-spa-index-sentinel"


@pytest.fixture
def reload_server():
    """把 FRONTEND_DIST 指向指定目录后重载 api.server，退出时还原 env 与模块状态。

    中文：SPA 托管分支在 import 期按模块级 _dist_path 一次性注册（server.py 静态托管段），
    因此必须"先设 env 再 reload"，不能只改模块属性；测试自造 dist 才能让 CI 干净
    checkout（无 frontend/dist）也同时覆盖 200 与 503 两种契约。
    """
    import hero_quant.api.server as srv

    original = os.environ.get("FRONTEND_DIST")

    def _load(dist):
        os.environ["FRONTEND_DIST"] = str(dist)
        return importlib.reload(srv)

    try:
        yield _load
    finally:
        if original is None:
            os.environ.pop("FRONTEND_DIST", None)
        else:
            os.environ["FRONTEND_DIST"] = original
        importlib.reload(srv)


def _make_dist(tmp_path: pathlib.Path) -> pathlib.Path:
    """最小可用 dist：index.html + assets/app.js。"""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<!doctype html><html><body><div id="root">'
        f"{INDEX_SENTINEL}</div>"
        '<script src="/assets/app.js"></script></body></html>',
        encoding="utf-8",
    )
    (dist / "assets" / "app.js").write_text("console.log('hero-spa');", encoding="utf-8")
    return dist


def test_spa_routes_serve_index_when_dist_present(tmp_path, reload_server):
    """有 dist：7 条前端路由 200 + 返回 index.html 内容（行为不变）。"""
    srv = reload_server(_make_dist(tmp_path))
    c = TestClient(srv.app)
    for path in FRONTEND_ROUTES:
        r = c.get(path, headers={"Accept": "text/html"})
        assert r.status_code == 200, f"{path} got {r.status_code} {r.text[:200]}"
        assert "text/html" in r.headers.get("content-type", ""), f"{path} not html"
        assert INDEX_SENTINEL in r.text, f"{path} 未返回 index.html: {r.text[:200]}"
    # 静态资源按文件直出
    js = c.get("/assets/app.js")
    assert js.status_code == 200, f"/assets/app.js got {js.status_code}"
    assert "hero-spa" in js.text
    # JSON 探针在 dist 就绪时报 mounted
    probe = c.get("/", headers={"Accept": "application/json"})
    assert probe.status_code == 200
    assert probe.json().get("status") == "ok"
    assert probe.json().get("frontend") == "mounted"


def test_spa_routes_503_when_dist_missing(tmp_path, reload_server):
    """无 dist（CI 干净 checkout）：7 条前端路由 503 + X-Frontend: missing。"""
    missing = tmp_path / "definitely-missing-dist"
    assert not missing.exists()
    srv = reload_server(missing)
    c = TestClient(srv.app)
    for path in FRONTEND_ROUTES:
        r = c.get(path, headers={"Accept": "text/html"})
        assert r.status_code == 503, f"{path} 缺 dist 必须 503，got {r.status_code}"
        assert r.headers.get("X-Frontend") == "missing", (
            f"{path} X-Frontend 应为 missing，got {r.headers.get('X-Frontend')}"
        )
        assert r.headers.get("Retry-After") == "30"
        assert "503" in r.text, f"{path} 503 说明页缺失：{r.text[:200]}"
    # JSON 探针必须看到 degraded，不能假装 ok
    probe = c.get("/", headers={"Accept": "application/json"})
    assert probe.status_code == 503
    body = probe.json()
    assert body.get("status") == "degraded"
    assert body.get("frontend") == "missing"
    assert probe.headers.get("X-Frontend") == "missing"
    # 缺失的静态资源始终 404（资源语义，不返回 index/503 说明页）
    static = c.get("/assets/definitely-missing.js")
    assert static.status_code == 404, f"missing .js must be 404, got {static.status_code}"


def test_spa_503_when_dist_present_but_index_missing(tmp_path, reload_server):
    """dist 在但未构建完（无 index.html）：503 + X-Frontend: index-missing。"""
    empty = tmp_path / "dist-empty"
    empty.mkdir()
    srv = reload_server(empty)
    c = TestClient(srv.app)
    for path in FRONTEND_ROUTES:
        r = c.get(path, headers={"Accept": "text/html"})
        assert r.status_code == 503, f"{path} 缺 index 必须 503，got {r.status_code}"
        assert r.headers.get("X-Frontend") == "index-missing"
        assert r.headers.get("Retry-After") == "30"
    probe = c.get("/", headers={"Accept": "application/json"})
    assert probe.status_code == 503
    assert probe.json().get("status") == "degraded"
    assert probe.json().get("frontend") == "index-missing"
    assert probe.headers.get("X-Frontend") == "index-missing"


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

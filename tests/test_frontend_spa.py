"""Wave F frontend SPA + E2E hardening TDD red."""
from fastapi.testclient import TestClient


def test_spa_routes_serve_html():
    from hero_quant.api.server import app

    c = TestClient(app)
    for path in ["/", "/dashboard", "/research", "/backtest", "/risk", "/settings", "/chat"]:
        r = c.get(path, headers={"Accept": "text/html"})
        assert r.status_code == 200, f"{path} got {r.status_code} {r.text[:200]}"
        # should be HTML with root div
        txt = r.text.lower()
        assert "<div id=\"root\"" in txt or "<!doctype html" in txt, f"{path} not html: {r.text[:200]}"
        assert "hero" in txt or "vite" in txt or "量化" in txt or "root" in txt


def test_health_and_metrics_and_wall_time():
    from hero_quant.api.server import app

    c = TestClient(app)
    # /live health JSON when not requesting html
    r = c.get("/live")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    # /ready
    assert c.get("/ready").status_code == 200
    # /metrics contains wall_time
    m = c.get("/metrics")
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
    # trace events SSE or JSON
    r = c.get("/v1/trace/events?offset=0", headers={"Accept": "text/event-stream"})
    assert r.status_code == 200
    # should be event-stream or json
    ct = r.headers.get("content-type", "")
    assert "event-stream" in ct or "json" in ct or "text" in ct


def test_frontend_dist_reused():
    import pathlib

    dist = pathlib.Path("frontend/dist")
    assert dist.is_dir(), "frontend/dist not found"
    assert (dist / "index.html").is_file(), "frontend/dist/index.html missing"
    # at least assets
    assets = list((dist / "assets").glob("*.js")) if (dist / "assets").exists() else []
    assert len(assets) >= 1, "frontend/dist/assets missing js"

# tests/test_api.py
def test_health_and_metrics():
    from fastapi.testclient import TestClient
    from hero_quant.api.server import app
    with TestClient(app) as c:
        live = c.get("/live")
        assert live.status_code == 200
        assert live.json() == {"status": "ok"}
        assert "default-src 'self'" in (live.headers.get("Content-Security-Policy") or "")
        m = c.get("/metrics")
        assert m.status_code == 200
        assert m.headers["content-type"].startswith("text/plain")
        assert b"# HELP" in m.content or b"hero_quant_requests_total" in m.content


def test_live():
    from fastapi.testclient import TestClient
    from hero_quant.api.server import app
    with TestClient(app) as c:
        r = c.get("/live")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


def test_metrics():
    from fastapi.testclient import TestClient
    from hero_quant.api.server import app
    with TestClient(app) as c:
        r = c.get("/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]

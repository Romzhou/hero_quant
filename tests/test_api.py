# tests/test_api.py
# T4-1 契约同步：/metrics 不再匿名暴露，无票据 401，有 X-Ticket 票据才 200。
def _metrics_ticket():
    from hero_quant.api import security
    return security.issue_ticket(ttl=60)


def test_health_and_metrics():
    from fastapi.testclient import TestClient
    from hero_quant.api.server import app
    with TestClient(app) as c:
        live = c.get("/live")
        assert live.status_code == 200
        assert live.json() == {"status": "ok"}
        assert "default-src 'self'" in (live.headers.get("Content-Security-Policy") or "")
        # 无票据匿名访问必须被拒绝（T4-1 /metrics 鉴权）
        m_anon = c.get("/metrics")
        assert m_anon.status_code == 401
        m = c.get("/metrics", headers={"X-Ticket": _metrics_ticket()})
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
        # 无票据匿名访问必须被拒绝（T4-1 /metrics 鉴权）
        r_anon = c.get("/metrics")
        assert r_anon.status_code == 401
        r = c.get("/metrics", headers={"X-Ticket": _metrics_ticket()})
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]

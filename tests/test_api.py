# tests/test_api.py
def test_health_and_metrics():
    from fastapi.testclient import TestClient
    from hero_quant.api.server import app
    c = TestClient(app)
    assert c.get("/live").status_code == 200
    assert c.get("/metrics").status_code == 200

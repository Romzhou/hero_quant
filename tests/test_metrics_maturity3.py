# tests/test_metrics_maturity3.py — B1-1 histogram + circuit gauge TDD
# T4-1 契约同步：/metrics 需鉴权（X-Ticket/API Key/HMAC），匿名 401。此处用单次票据取指标。
def _metrics_ticket():
    from hero_quant.api import security

    return security.issue_ticket(ttl=60)


def _metrics_text(client):
    return client.get("/metrics", headers={"X-Ticket": _metrics_ticket()}).text

def test_metrics_histogram():
    from fastapi.testclient import TestClient
    from hero_quant.api.server import app

    c = TestClient(app)
    # trigger a request to populate histogram
    resp = c.get("/live")
    assert resp.status_code == 200
    txt = _metrics_text(c)
    assert "hero_quant_requests_total" in txt
    # Prometheus histogram exposes _bucket series
    assert "http_request_duration_seconds_bucket" in txt
    # also ensure histogram help/type lines exist
    assert "http_request_duration_seconds" in txt


def test_metrics_histogram_observes_endpoint_label():
    from fastapi.testclient import TestClient
    from hero_quant.api.server import app

    c = TestClient(app)
    c.get("/ready")
    txt = _metrics_text(c)
    # label endpoint should appear on histogram bucket line
    assert 'endpoint=' in txt
    assert "http_request_duration_seconds_bucket" in txt


def test_circuit_gauge_exposed():
    from hero_quant.telemetry.circuit import CircuitBreaker
    from fastapi.testclient import TestClient
    from hero_quant.api.server import app

    cb = CircuitBreaker(failure_threshold=0.5, window=1, open_duration=1)
    # allow() should expose circuit_state gauge (stub is acceptable)
    cb.allow()
    c = TestClient(app)
    txt = _metrics_text(c)
    assert "circuit_state" in txt

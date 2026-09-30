from types import SimpleNamespace

from fastapi.testclient import TestClient

from hero_quant.api import security
from hero_quant.api.server import app


def test_query_ticket_endpoint_issues_ticket_for_one_stream():
    client = TestClient(app)

    response = client.post("/v1/query/ticket")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"ticket", "expires_in"}
    assert isinstance(payload["ticket"], str) and payload["ticket"]
    assert payload["expires_in"] == 60

    # T4-1 契约同步：ticket 只走 X-Ticket header，?ticket= 不再生效
    via_query_param = client.get("/v1/query/stream", params={"ticket": payload["ticket"]})
    assert via_query_param.status_code == 403
    stream = client.get("/v1/query/stream", headers={"X-Ticket": payload["ticket"]})
    assert stream.status_code == 200


def test_query_stream_ticket_is_single_use():
    client = TestClient(app)
    ticket = security.issue_ticket(ttl=60)

    first = client.get("/v1/query/stream", params={"q": "600519.SH"}, headers={"X-Ticket": ticket})
    assert first.status_code == 200
    assert "text/event-stream" in first.headers["content-type"]

    replay = client.get("/v1/query/stream", params={"q": "600519.SH"}, headers={"X-Ticket": ticket})
    assert replay.status_code == 403


def test_expired_ticket_is_rejected_and_cleaned(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(security, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    ticket = security.issue_ticket(ttl=60)
    redis_client = security._get_redis_for_ticket()
    if redis_client is not None:
        # Redis语义：票据存于 hero:ticket:{t}，内存 _tickets 为空是预期的
        key = f"{security._REDIS_TICKET_PREFIX}{ticket}"
        assert redis_client.exists(key) == 1
        # Redis TTL走真实时间，显式删除模拟过期后的清理语义
        redis_client.delete(key)
        # T4-1 契约同步：ticket 只走 X-Ticket header
        response = TestClient(app).get("/v1/query/stream", headers={"X-Ticket": ticket})

        assert response.status_code == 403
        assert redis_client.exists(key) == 0
    else:
        # 内存回退分支旧断言（无 Redis 时）
        assert ticket in security._tickets

        clock[0] = 161.0
        # T4-1 契约同步：ticket 只走 X-Ticket header
        response = TestClient(app).get("/v1/query/stream", headers={"X-Ticket": ticket})

        assert response.status_code == 403
        assert ticket not in security._tickets


def test_existing_health_and_trace_sse_behavior_remains_available():
    client = TestClient(app)

    assert client.get("/live").status_code == 200
    trace = client.get("/v1/trace/events", headers={"Accept": "text/event-stream"})
    assert trace.status_code == 200
    assert "text/event-stream" in trace.headers["content-type"]

"""O线 fuzz 边缘行为回归锁：把 live 模糊测试观测到的 fail-closed 行为锁死。

覆盖：ticket 非法 scope 400 / 方法混淆 404-405 / 无票-伪票-复用 401 /
trace offset 负数 400-非数字 422 / risk 降级带因 / metrics 无票 401 /
replay_path 穿越 400。全部无 500 断言。
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def _client():
    from hero_quant.api.server import app

    return TestClient(app)


def test_o_ticket_illegal_scope_400():
    c = _client()
    for sc in ("admin", "QUERY", "", "metrics;DROP", "1"):
        r = c.post("/v1/query/ticket", params={"scope": sc})
        assert r.status_code == 400, (sc, r.status_code)


def test_o_ticket_scope_whitespace_normalized():
    c = _client()
    r = c.post("/v1/query/ticket", params={"scope": "query "})
    assert r.status_code == 200  # _normalize_scope strip 后放行（观测行为，锁死防抖动）


def test_o_ticket_method_confusion():
    c = _client()
    # GET 落入 SPA catch-all v1/ 守卫 -> 404（观测行为）；其余方法 405
    assert c.get("/v1/query/ticket").status_code == 404
    assert c.put("/v1/query/ticket").status_code == 405
    assert c.delete("/v1/query/ticket").status_code == 405


def test_o_query_ticket_auth_closed():
    c = _client()
    assert c.get("/v1/query", params={"q": "hi"}).status_code == 401
    assert c.get("/v1/query", params={"q": "hi"}, headers={"X-Ticket": "forged"}).status_code == 401
    t = c.post("/v1/query/ticket").json()["ticket"]
    assert c.get("/v1/query", params={"q": "hi"}, headers={"X-Ticket": t}).status_code == 200
    assert c.get("/v1/query", params={"q": "hi"}, headers={"X-Ticket": t}).status_code == 401


def test_o_trace_offset_edges():
    c = _client()
    assert c.get("/v1/trace/events", params={"offset": -1}).status_code == 400
    assert c.get("/v1/trace/events", params={"offset": "abc"}).status_code == 422
    r = c.get("/v1/trace/events", params={"offset": 999999999999})
    assert r.status_code == 200
    body = r.json()
    assert body["offset"] == 999999999999
    assert isinstance(body["events"], list)


def test_o_risk_degraded_has_reason():
    c = _client()
    r = c.get("/v1/risk/summary")
    assert r.status_code == 200
    body = r.json()
    if body.get("degraded") is True:
        assert body.get("reason"), "degraded:true 必须带 reason（AGENTS §6）"


def test_o_metrics_no_ticket_401():
    c = _client()
    assert c.get("/metrics").status_code == 401


def test_o_query_replay_traversal_400():
    c = _client()
    t = c.post("/v1/query/ticket").json()["ticket"]
    h = {"X-Ticket": t}
    assert c.get("/v1/query", params={"q": "x", "replay_path": "../../etc/passwd"}, headers=h).status_code == 400
    t2 = c.post("/v1/query/ticket").json()["ticket"]
    assert c.get("/v1/query", params={"q": "x", "trace_dir": "/etc"}, headers={"X-Ticket": t2}).status_code == 400


def test_o_backtest_method_confusion_405():
    c = _client()
    assert c.post("/v1/backtest/metrics.json").status_code == 405
    assert c.put("/v1/backtest/drawdowns.json").status_code == 405
    assert c.post("/v1/trace/events").status_code == 405

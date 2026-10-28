"""Task9 TDD: /ready degraded — PG down -> 503 + degraded:true + reason（行为断言，无恒真分支）。"""
from fastapi.testclient import TestClient


def _clear_ready_cache():
    import hero_quant.api.server as srv

    srv._READY_CACHE.clear()


def test_ready_ok_and_degraded_fields():
    from hero_quant.api.server import app

    _clear_ready_cache()
    c = TestClient(app)
    r = c.get("/ready")
    j = r.json()
    # 状态码与 body 必须自洽：ok <-> 200，degraded <-> 503；不再接受“200/503 都行”的恒真断言。
    assert j["status"] in ("ok", "degraded")
    assert j["degraded"] is (j["status"] == "degraded")
    assert r.status_code == (200 if j["status"] == "ok" else 503)
    assert "pg" in j and isinstance(j["pg"], bool)
    assert "checkpoint" in j and j["checkpoint"] in ("pg", "memory")
    assert "billing" in j and j["billing"] in ("pg", "memory")
    # checkpoint 非真 PG = 无持久化，必须 degraded 且 reason 明写 checkpoint
    if j["checkpoint"] != "pg":
        assert j["degraded"] is True
        assert "checkpoint" in j.get("reasons", {})


def test_ready_degraded_when_pg_down(monkeypatch):
    """mock checkpoint PG 探针失败 -> 必须 503 + degraded + pg=false + memory。"""
    import hero_quant.api.server as srv

    _clear_ready_cache()

    def fake_down():
        return False, "memory"

    monkeypatch.setattr(srv, "_check_checkpoint_pg", fake_down)
    from fastapi.testclient import TestClient

    from hero_quant.api.server import app

    c = TestClient(app)
    r = c.get("/ready")
    j = r.json()
    assert r.status_code == 503, f"checkpoint 不可用必须 503，got {r.status_code}"
    assert j["status"] == "degraded" and j["degraded"] is True
    assert j["checkpoint"] == "memory"
    assert j["pg"] is False
    assert "checkpoint" in j.get("reasons", {})


def test_ready_pg_probe_uses_checkpoint_and_billing():
    """/ready 必须同时暴露 checkpoint 与 billing 探针结果，且 degraded 时 reason 非空。"""
    from hero_quant.api.server import app

    _clear_ready_cache()
    c = TestClient(app)
    r = c.get("/ready")
    j = r.json()
    assert "billing" in j
    assert j["billing"] in ("pg", "memory")
    if j["degraded"]:
        assert j.get("reasons"), "degraded:true 必须带具体 reasons，不许无因降级"
        assert isinstance(j["reasons"], dict) and j["reasons"]
    else:
        assert j["checkpoint"] == "pg"

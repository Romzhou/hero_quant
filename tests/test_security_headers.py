"""B2-2 TDD: 最小 CSP + DNS 环回校验"""

from fastapi.testclient import TestClient
from hero_quant.api.server import app


def _client():
    return TestClient(app)


def test_csp_header_contains_default_src_self():
    c = _client()
    resp = c.get("/live")
    assert resp.status_code == 200
    csp = resp.headers.get("Content-Security-Policy", "")
    # 要求包含 default-src 'self'
    assert "default-src" in csp
    assert "'self'" in csp

    # GET / 也应含同样头（即使无前端，404 上也应由中间件注入；但这里测 /live 兜底同时测 /）
    resp_root = c.get("/")
    csp_root = resp_root.headers.get("Content-Security-Policy", "")
    assert "default-src" in csp_root
    assert "'self'" in csp_root


def test_x_frame_options_deny():
    c = _client()
    resp = c.get("/live")
    assert resp.headers.get("X-Frame-Options") == "DENY"
    resp_root = c.get("/")
    assert resp_root.headers.get("X-Frame-Options") == "DENY"


def test_rejects_untrusted_host():
    c = _client()
    # 非法 Host 必须 403
    resp = c.get("/live", headers={"host": "evil.com"})
    assert resp.status_code == 403

    # 正常环回 Host 仍放行
    resp_ok = c.get("/live", headers={"host": "localhost"})
    assert resp_ok.status_code == 200
    resp_ok2 = c.get("/live", headers={"host": "127.0.0.1"})
    assert resp_ok2.status_code == 200
    # TestClient 默认 host testserver 也应放行
    resp_default = c.get("/live")
    assert resp_default.status_code == 200

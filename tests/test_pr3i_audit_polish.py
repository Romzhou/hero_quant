"""PR3-I TDD: 审计/PII/安全头 + 窄化 + 文档抛光。

范围只碰 api/middleware/ 新建、data/registry.py 窄化、
tests/test_otel_maturity3.py 弱断言、docs/README。
"""
from __future__ import annotations


def test_trace_middleware_passthrough_x_request_id():
    """TraceId 中间件必须透传 x-request-id（请求头 -> 响应头 + contextvars）。"""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from hero_quant.api.middleware.trace import TraceIdMiddleware, get_trace_id

    app = FastAPI()
    app.add_middleware(TraceIdMiddleware)

    @app.get("/ping")
    def ping():
        return JSONResponse({"trace": get_trace_id()})

    client = TestClient(app)
    rid = "pr3i-trace-123"
    resp = client.get("/ping", headers={"x-request-id": rid})
    assert resp.status_code == 200
    # 透传：响应头回显同一 ID（大小写由 Starlette 归一，取值比对）
    assert resp.headers.get("x-request-id") == rid or resp.headers.get("X-Request-ID") == rid
    assert resp.headers.get("x-trace-id") == rid or resp.headers.get("X-Trace-Id") == rid
    # contextvars 透传到 handler
    assert resp.json()["trace"] == rid


def test_security_headers_nosniff_frame_deny():
    """SecurityHeaders 必须含 nosniff / frame-deny。"""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from hero_quant.api.middleware.security_headers import SecurityHeadersMiddleware

    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/ping")
    def ping():
        return JSONResponse({"ok": True})

    client = TestClient(app)
    resp = client.get("/ping")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"


def test_pii_fernet_roundtrip():
    """PII Fernet 脱敏 roundtrip：加密->解密还原；无依赖时回退掩码。"""
    from hero_quant.api.middleware import pii as pii_mod

    # mask 回退路径恒可用
    masked = pii_mod.mask_pii("13800000000")
    assert "138" in masked and "0000" in masked and "****" in masked.replace("*", "*")

    # Fernet 路径：若 cryptography 可用则 roundtrip
    if getattr(pii_mod, "CRYPTO_AVAILABLE", False):
        from cryptography.fernet import Fernet

        import os

        key = Fernet.generate_key().decode()
        os.environ["PII_ENCRYPTION_KEY"] = key
        # 重置懒加载缓存
        try:
            pii_mod._PII_KEY = None
        except AttributeError:
            pass
        enc = pii_mod.pii_encrypt("13800000000")
        assert enc != "13800000000"
        assert pii_mod.pii_decrypt(enc) == "13800000000"
    else:
        enc = pii_mod.pii_encrypt("13800000000")
        assert enc.startswith("!NOENC!")


def test_registry_narrowed_still_raises():
    """registry 窄化后非法输入仍抛 ValueError/TypeError（不被吞掉）。"""
    import pathlib
    import re

    from hero_quant.data.registry import MarketDataRegistry

    # 非法 loader：markets 类型错误 -> TypeError；缺方法 -> ValueError
    reg = MarketDataRegistry()
    try:
        reg.register(object())
        raise AssertionError("expected ValueError/TypeError for object()")
    except (ValueError, TypeError):
        pass

    class BadMarkets:
        markets = "US"  # 非法：应为 list
        unit = "shares"

        def get_bars(self, symbol, start, end, interval="1d"):
            return [], None

    try:
        reg.register(BadMarkets())
        raise AssertionError("expected TypeError for bad markets")
    except (ValueError, TypeError):
        pass

    # 裸 except 存量 <5
    txt = pathlib.Path("src/hero_quant/data/registry.py").read_text(encoding="utf-8")
    bare = re.findall(r"except\s+Exception\b", txt)
    assert len(bare) < 5, f"裸 except 仍有 {len(bare)} 处，期望 <5"

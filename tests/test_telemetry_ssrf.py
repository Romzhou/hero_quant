"""Task 8 TDD: telemetry/otel.py SSRF 加固验证。

覆盖：private/loopback/link_local/reserved/multicast 统一拦截、
非 IP 主机二次解析、userinfo/port 校验、_redact_dsn 脱敏、
HERO_OTEL_MODE=disabled 默认。
"""
from __future__ import annotations

import pytest


def _is_allowed(endpoint: str) -> bool:
    """统一入口：优先模块级 _is_allowed_endpoint，兼容类方法。"""
    try:
        from hero_quant.telemetry.otel import _is_allowed_endpoint as fn

        return fn(endpoint)
    except ImportError:
        from hero_quant.telemetry.otel import SessionTelemetryCoordinator

        return SessionTelemetryCoordinator(mode="private")._validate_endpoint(endpoint)


def test_ssrf_private_ip_blocked():
    """私网/环回/链路本地/保留/组播统一拦截 + 元数据地址。"""
    # 云元数据
    assert _is_allowed("http://169.254.169.254/latest/meta-data/") is False
    assert _is_allowed("http://metadata.google.internal/") is False
    # 子域亦拦截
    assert _is_allowed("http://foo.metadata.google.internal/v1/traces") is False
    assert _is_allowed("http://METADATA.GOOGLE.INTERNAL/v1/traces") is False
    # 私网段
    assert _is_allowed("http://10.0.0.1:4317/v1/traces") is False
    assert _is_allowed("http://192.168.1.1/v1/traces") is False
    assert _is_allowed("http://172.16.5.4/v1/traces") is False
    # 环回
    assert _is_allowed("http://127.0.0.1:4317/v1/traces") is False
    assert _is_allowed("http://[::1]/v1/traces") is False
    # link_local 169.254.0.0/16
    assert _is_allowed("http://169.254.10.20/v1/traces") is False
    assert _is_allowed("http://[fe80::1]/v1/traces") is False
    # 组播 / 保留段（按 ip.is_multicast / is_reserved 覆盖）
    assert _is_allowed("http://224.0.0.1/v1/traces") is False
    assert _is_allowed("http://192.0.2.1/v1/traces") is False  # TEST-NET-1, is_reserved 在新版 Python 可能为 False，但 is_private 已覆盖，仍应拒绝


def test_ssrf_userinfo_port_rejected():
    """含 userinfo 的 URL 必须拒绝（凭证投递 SSRF）。公网主机亦不例外。"""
    assert _is_allowed("http://user:pass@10.0.0.1:4317/v1/traces") is False
    assert _is_allowed("http://user:pass@8.8.8.8/v1/traces") is False
    assert _is_allowed("http://user:pass@example.com/v1/traces") is False
    assert _is_allowed("http://evil:123@collector.test:4318/v1/logs") is False


def test_ssrf_non_ip_second_resolution_blocked(monkeypatch):
    """非 IP 主机二次 DNS 解析到私网亦需拦截。"""
    import socket

    def fake_getaddrinfo(host, port, *a, **kw):
        if host == "evil.example":
            # 模拟解析到私网 10.0.0.1
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 0))]
        if host == "good.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert _is_allowed("http://evil.example/v1/traces") is False
    assert _is_allowed("http://good.example/v1/traces") is True


def test_ssrf_valid_public_allowed(monkeypatch):
    """公网合法端点应放行。"""
    # 隔离本机 DNS 劫持：mock 解析到公网 8.8.8.8，保证与二次解析逻辑解耦
    import socket

    orig = socket.getaddrinfo

    def _fake_allow(host, *a, **kw):
        try:
            # 若测试已显式 mock，保持之；否则让真实解析或兜底
            return orig(host, *a, **kw)
        except Exception:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]

    def fake_getaddrinfo(host, port, *a, **kw):
        # otel-collector 无真实 DNS，单测中视为内网服务名放行
        if host in ("otel-collector", "collector.test", "example.com"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]
        return _fake_allow(host, port, *a, **kw)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert _is_allowed("http://otel-collector:4318/v1/logs") is True
    assert _is_allowed("http://collector.test:4318/v1/logs") is True
    assert _is_allowed("https://example.com/v1/traces") is True
    assert _is_allowed("https://8.8.8.8/v1/traces") is True


def test_ssrf_invalid_scheme_rejected():
    assert _is_allowed("ftp://example.com/v1/traces") is False
    assert _is_allowed("http:///v1/traces") is False
    assert _is_allowed("not-a-url") is False


def test_ssrf_log_redact(monkeypatch):
    """日志脱敏：含口令的 endpoint 不得明文出现在日志中，复用 settings._redact_dsn。"""
    import logging

    from hero_quant.config.settings import _redact_dsn

    # 复用一致性：脱敏函数应对含口令 DSN 生效
    redacted = _redact_dsn("http://user:secret@example.com/v1/logs")
    assert "secret" not in redacted
    assert "***" in redacted

    # 校验 otel 警告日志使用脱敏后 endpoint
    from hero_quant.telemetry.otel import SessionTelemetryCoordinator

    coord = SessionTelemetryCoordinator(mode="private")
    captured = {}

    def fake_warning(msg, *a, **kw):
        captured["msg"] = msg
        captured["kw"] = kw

    monkeypatch.setattr("hero_quant.telemetry.otel.logger.warning", fake_warning)
    # 触发阻塞路径：私有 IP
    result = coord._validate_endpoint("http://user:secret@10.0.0.1/v1/traces")
    assert result is False
    # 日志中的 endpoint 字段应为脱敏值，不含 secret
    endpoint_logged = str(captured.get("kw", {}).get("endpoint", "")) if isinstance(captured.get("kw"), dict) else ""
    # 兼容 structlog 调用风格：warning("msg", endpoint=...)
    if not endpoint_logged and isinstance(captured.get("kw"), dict):
        endpoint_logged = str(captured["kw"].get("endpoint", ""))
    # 若实现为 warning("msg %s", ..., endpoint=redacted) 则在 kw 中
    # 兜底：检查所有捕获字符串
    all_text = str(captured)
    assert "secret" not in all_text
    assert "secret" not in endpoint_logged or endpoint_logged == ""


def test_otel_mode_disabled_default(monkeypatch):
    """HERO_OTEL_MODE 未设置时默认 disabled，export 不出网。"""
    monkeypatch.delenv("HERO_OTEL_MODE", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    import importlib

    import hero_quant.telemetry.otel as otel_mod

    importlib.reload(otel_mod)
    from hero_quant.telemetry.otel import get_otel_mode, SessionTelemetryCoordinator
    import urllib.request

    assert get_otel_mode() == "disabled"
    coord = SessionTelemetryCoordinator()  # 默认应为 disabled
    assert coord.mode == "disabled"
    assert coord.is_enabled() is False

    called = {"hit": False}

    def fake_urlopen(req, timeout):
        called["hit"] = True
        raise AssertionError("disabled mode should not call urlopen")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4318/v1/logs")
    coord.export({"event": "x"})
    assert called["hit"] is False

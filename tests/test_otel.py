import builtins
import json
import urllib.request


def test_otel_sharing_modes(monkeypatch):
    monkeypatch.setenv("HERO_OTEL_MODE","disabled")
    from hero_quant.telemetry.otel import get_otel_mode
    assert get_otel_mode()=="disabled"
    from hero_quant.telemetry.otel import SessionTelemetryCoordinator
    c = SessionTelemetryCoordinator(mode="disabled")
    assert c.sharing()=="disabled"


def test_otel_unknown_mode_fail_closed(monkeypatch):
    monkeypatch.setenv("HERO_OTEL_MODE", "weird_unknown_xyz")
    from hero_quant.telemetry.otel import get_otel_mode, SessionTelemetryCoordinator
    # get_otel_mode should delegate to _normalize_mode -> disabled, not raw unknown
    assert get_otel_mode() == "disabled"
    c = SessionTelemetryCoordinator(mode="weird_unknown_xyz")
    assert c.mode == "disabled"
    assert c.sharing() == "disabled"
    assert c.is_enabled() is False


def test_otel_endpoint_validation_blocks_metadata(monkeypatch):
    monkeypatch.setenv("HERO_OTEL_MODE", "shared")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://169.254.169.254/latest/meta-data/")
    import urllib.request
    called = {"hit": False}

    def fake_urlopen(req, timeout):
        called["hit"] = True
        raise AssertionError("should not be called for blocked endpoint")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    from hero_quant.telemetry.otel import SessionTelemetryCoordinator
    SessionTelemetryCoordinator(mode="shared").export({"event": "x"})
    assert called["hit"] is False


def test_otel_provider_singleton(monkeypatch):
    monkeypatch.setenv("HERO_OTEL_MODE", "shared")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4318/v1/logs")
    import hero_quant.telemetry.otel as otel_mod
    # reset singleton
    otel_mod._OTEL_CACHED_PROVIDER = None
    otel_mod._OTEL_CACHED_ENDPOINT = None
    # mock SDK to avoid real network
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ImportError("mock no sdk")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    import urllib.request, json

    captured = {}

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_open(req, timeout):
        captured["url"] = req.full_url
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    from hero_quant.telemetry.otel import SessionTelemetryCoordinator
    c = SessionTelemetryCoordinator(mode="shared")
    c.export({"a": 1})
    c.export({"a": 2})
    assert captured["url"] == "http://collector.test:4318/v1/logs"


def test_shared_without_sdk_exports_otlp_logs_json(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.test:4318/v1/logs")

    real_import = builtins.__import__

    def reject_opentelemetry(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise ImportError("OpenTelemetry SDK unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", reject_opentelemetry)
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def capture_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", capture_urlopen)

    from hero_quant.telemetry.otel import SessionTelemetryCoordinator

    payload = {"event": "heartbeat", "value": 7}
    SessionTelemetryCoordinator(mode="shared").export(payload)

    request = captured["request"]
    body = json.loads(request.data.decode("utf-8"))
    assert request.full_url == "http://collector.test:4318/v1/logs"
    assert captured["timeout"] == 0.5
    assert set(body) == {"resourceLogs"}
    assert body["resourceLogs"]
    scope_logs = body["resourceLogs"][0]["scopeLogs"]
    log_records = scope_logs[0]["logRecords"]
    assert log_records[0]["body"] == {"stringValue": json.dumps(payload)}

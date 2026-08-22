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

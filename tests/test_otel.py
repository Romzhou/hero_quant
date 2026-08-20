# tests/test_otel.py
def test_otel_sharing_modes(monkeypatch):
    monkeypatch.setenv("HERO_OTEL_MODE","disabled")
    from hero_quant.telemetry.otel import get_otel_mode
    assert get_otel_mode()=="disabled"
    from hero_quant.telemetry.otel import SessionTelemetryCoordinator
    c = SessionTelemetryCoordinator(mode="disabled")
    assert c.sharing()=="disabled"

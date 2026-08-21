"""B1-2 TDD: otel stub -> BatchLogRecordProcessor minimal (fallback urllib)."""
import sys
import types
import unittest.mock as mock


def _reload_otel():
    import importlib

    if "hero_quant.telemetry.otel" in sys.modules:
        importlib.reload(sys.modules["hero_quant.telemetry.otel"])
    else:
        import hero_quant.telemetry.otel  # noqa: F401
    from hero_quant.telemetry.otel import SessionTelemetryCoordinator

    return SessionTelemetryCoordinator


def _cleanup_fake_otel():
    # remove fakes we injected if they are MagicMock-backed; keep real api if present
    for m in list(sys.modules.keys()):
        if m.startswith("opentelemetry.sdk") or m.startswith("opentelemetry.exporter"):
            sys.modules.pop(m, None)
    # _logs fake that is MagicMock-backed (has no real file)
    mod = sys.modules.get("opentelemetry._logs")
    if mod is not None and isinstance(getattr(mod, "get_logger", None), mock.MagicMock):
        sys.modules.pop("opentelemetry._logs", None)


def test_export_no_endpoint_is_noop(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("HERO_OTEL_MODE", "private")
    SessionTelemetryCoordinator = _reload_otel()
    coord = SessionTelemetryCoordinator(mode="private")
    with mock.patch("urllib.request.urlopen") as mock_urlopen:
        coord.export({"path": "/live", "trace_id": "t1"})
        mock_urlopen.assert_not_called()
        assert True
    _cleanup_fake_otel()


def test_export_with_endpoint_uses_batch_not_throw(monkeypatch):
    monkeypatch.setenv("HERO_OTEL_MODE", "private")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318/v1/logs")
    fake_logger_provider_cls = mock.MagicMock(name="LoggerProvider")
    fake_provider_instance = mock.MagicMock(name="provider")
    fake_logger_provider_cls.return_value = fake_provider_instance
    fake_otel_logger = mock.MagicMock(name="otel_logger")
    fake_provider_instance.get_logger.return_value = fake_otel_logger
    fake_batch_cls = mock.MagicMock(name="BatchLogRecordProcessor")
    fake_exporter_cls = mock.MagicMock(name="OTLPLogExporter")

    sdk_logs_mod = types.ModuleType("opentelemetry.sdk._logs")
    sdk_logs_mod.LoggerProvider = fake_logger_provider_cls
    sdk_logs_export_mod = types.ModuleType("opentelemetry.sdk._logs.export")
    sdk_logs_export_mod.BatchLogRecordProcessor = fake_batch_cls
    exporter_mod = types.ModuleType("opentelemetry.exporter.otlp.proto.http._log_exporter")
    exporter_mod.OTLPLogExporter = fake_exporter_cls
    api_logs_mod = types.ModuleType("opentelemetry._logs")
    api_logs_mod.get_logger = mock.MagicMock(return_value=fake_otel_logger)

    # use patch.dict so that after test modules are restored
    original_modules = dict(sys.modules)
    try:
        for name in [
            "opentelemetry",
            "opentelemetry.sdk",
            "opentelemetry.sdk._logs",
            "opentelemetry.sdk._logs.export",
            "opentelemetry.exporter",
            "opentelemetry.exporter.otlp",
            "opentelemetry.exporter.otlp.proto",
            "opentelemetry.exporter.otlp.proto.http",
            "opentelemetry.exporter.otlp.proto.http._log_exporter",
            "opentelemetry._logs",
        ]:
            if name not in sys.modules:
                sys.modules[name] = types.ModuleType(name)
        sys.modules["opentelemetry.sdk._logs"] = sdk_logs_mod
        sys.modules["opentelemetry.sdk._logs.export"] = sdk_logs_export_mod
        sys.modules["opentelemetry.exporter.otlp.proto.http._log_exporter"] = exporter_mod
        sys.modules["opentelemetry._logs"] = api_logs_mod

        SessionTelemetryCoordinator = _reload_otel()
        coord = SessionTelemetryCoordinator(mode="private")
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            coord.export({"path": "/live", "trace_id": "t1"})
            assert fake_exporter_cls.called, "OTLPLogExporter should be instantiated via Batch path"
            assert fake_batch_cls.called, "BatchLogRecordProcessor should be used when endpoint set"
            mock_urlopen.assert_not_called()
            fake_provider_instance.add_log_record_processor.assert_called()
            assert fake_otel_logger.emit.called or fake_provider_instance.get_logger.called
    finally:
        # restore sys.modules to original, keeping real api if it existed before
        sys.modules.clear()
        sys.modules.update(original_modules)
        _cleanup_fake_otel()


def test_export_offline_safe_when_batch_raises(monkeypatch):
    monkeypatch.setenv("HERO_OTEL_MODE", "private")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318/v1/logs")
    fake_logger_provider_cls = mock.MagicMock(name="LoggerProvider")
    fake_provider_instance = mock.MagicMock(name="provider")
    fake_provider_instance.get_logger.side_effect = RuntimeError("collector offline")
    fake_provider_instance.add_log_record_processor.side_effect = RuntimeError("offline")
    fake_logger_provider_cls.return_value = fake_provider_instance
    fake_batch_cls = mock.MagicMock(side_effect=RuntimeError("batch fail"))
    fake_exporter_cls = mock.MagicMock(side_effect=RuntimeError("exporter fail"))

    sdk_logs_mod = types.ModuleType("opentelemetry.sdk._logs")
    sdk_logs_mod.LoggerProvider = fake_logger_provider_cls
    sdk_logs_export_mod = types.ModuleType("opentelemetry.sdk._logs.export")
    sdk_logs_export_mod.BatchLogRecordProcessor = fake_batch_cls
    exporter_mod = types.ModuleType("opentelemetry.exporter.otlp.proto.http._log_exporter")
    exporter_mod.OTLPLogExporter = fake_exporter_cls
    api_logs_mod = types.ModuleType("opentelemetry._logs")
    api_logs_mod.get_logger = mock.MagicMock(return_value=mock.MagicMock())

    original_modules = dict(sys.modules)
    try:
        sys.modules["opentelemetry.sdk._logs"] = sdk_logs_mod
        sys.modules["opentelemetry.sdk._logs.export"] = sdk_logs_export_mod
        sys.modules["opentelemetry.exporter.otlp.proto.http._log_exporter"] = exporter_mod
        sys.modules["opentelemetry._logs"] = api_logs_mod
        for name in [
            "opentelemetry",
            "opentelemetry.sdk",
            "opentelemetry.exporter",
            "opentelemetry.exporter.otlp",
            "opentelemetry.exporter.otlp.proto",
            "opentelemetry.exporter.otlp.proto.http",
        ]:
            if name not in sys.modules:
                sys.modules[name] = types.ModuleType(name)
        SessionTelemetryCoordinator = _reload_otel()
        coord = SessionTelemetryCoordinator(mode="private")
        coord.export({"path": "/live"})
        assert True
    finally:
        sys.modules.clear()
        sys.modules.update(original_modules)
        _cleanup_fake_otel()


def test_export_fallback_to_urllib_when_sdk_missing(monkeypatch):
    monkeypatch.setenv("HERO_OTEL_MODE", "private")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318/v1/logs")
    for m in list(sys.modules.keys()):
        if m.startswith("opentelemetry.sdk") or m.startswith("opentelemetry.exporter"):
            sys.modules.pop(m, None)
    # keep api but remove _logs fake if magic
    if isinstance(getattr(sys.modules.get("opentelemetry._logs"), "get_logger", None), mock.MagicMock):
        sys.modules.pop("opentelemetry._logs", None)
    SessionTelemetryCoordinator = _reload_otel()
    coord = SessionTelemetryCoordinator(mode="private")
    with mock.patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = mock.MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = lambda s, *a: False
        mock_urlopen.return_value = mock_resp
        coord.export({"path": "/live"})
        assert mock_urlopen.called, "fallback urllib should be used when BatchLogRecordProcessor unavailable"
    with mock.patch("urllib.request.urlopen", side_effect=RuntimeError("offline")):
        coord.export({"path": "/live"})
        assert True
    _cleanup_fake_otel()


def test_export_disabled_never_exports(monkeypatch):
    monkeypatch.setenv("HERO_OTEL_MODE", "disabled")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318/v1/logs")
    fake_batch_cls = mock.MagicMock(name="BatchLogRecordProcessor")
    sdk_logs_export_mod = types.ModuleType("opentelemetry.sdk._logs.export")
    sdk_logs_export_mod.BatchLogRecordProcessor = fake_batch_cls
    original = sys.modules.get("opentelemetry.sdk._logs.export")
    try:
        sys.modules["opentelemetry.sdk._logs.export"] = sdk_logs_export_mod
        SessionTelemetryCoordinator = _reload_otel()
        coord = SessionTelemetryCoordinator(mode="disabled")
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            coord.export({"path": "/live"})
            mock_urlopen.assert_not_called()
            fake_batch_cls.assert_not_called()
    finally:
        if original is not None:
            sys.modules["opentelemetry.sdk._logs.export"] = original
        else:
            sys.modules.pop("opentelemetry.sdk._logs.export", None)
        _cleanup_fake_otel()

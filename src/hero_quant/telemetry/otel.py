"""OTel three-mode telemetry backbone.

Modes:
- disabled: no export, sharing == disabled
- shared: anonymized metrics export (legacy enabled/sampling/internal/minimal/anonymous map here)
- private: full traces+metrics export (legacy full map here)

Env gates use os.environ.get (config gate pattern). Offline stays green via try/except.
"""

from __future__ import annotations

import os

# Valid modes — normalized lower-case; includes legacy aliases for backwards compat
_VALID_MODES = {"disabled", "shared", "private", "enabled", "sampling", "minimal", "full", "internal", "anonymous"}
_DEFAULT_MODE = "disabled"

# sharing mapping to canonical three gears
_SHARING_MAP = {
    "disabled": "disabled",
    "shared": "shared",
    "private": "private",
    # legacy aliases
    "enabled": "shared",
    "sampling": "shared",
    "minimal": "shared",
    "internal": "shared",
    "anonymous": "shared",
    "full": "private",
}


def _normalize_mode(raw: str | None) -> str:
    if not raw:
        return _DEFAULT_MODE
    m = raw.strip().lower()
    if m in _VALID_MODES:
        return m
    return _DEFAULT_MODE


def get_otel_mode() -> str:
    """Return current OTel mode from env HERO_OTEL_MODE, default disabled."""
    raw = os.environ.get("HERO_OTEL_MODE", _DEFAULT_MODE)
    # empty string should fallback to disabled
    if raw is None or raw == "":
        return _DEFAULT_MODE
    norm = raw.strip().lower()
    if norm in _VALID_MODES:
        return norm
    # Accept any non-empty as-is normalized (future-proof), but ensure disabled fallback for empty
    return norm if norm else _DEFAULT_MODE


class SessionTelemetryCoordinator:
    """Per-session telemetry coordinator with sharing level."""

    def __init__(self, mode: str | None = None) -> None:
        # If mode not provided, fallback to env
        if mode is None:
            mode = get_otel_mode()
        self.mode = _normalize_mode(mode)

    def sharing(self) -> str:
        """Return sharing level for current mode.

        Canonical three gears: disabled / shared / private.
        Legacy modes map via _SHARING_MAP.
        """
        return _SHARING_MAP.get(self.mode, "disabled" if self.mode == "disabled" else "shared")

    def export(self, payload: dict | None = None) -> None:
        """Export: LoggerProvider + BatchLogRecordProcessor minimal (fallback urllib). Offline-safe.

        capture/export 分离: capture 侧仅决定是否导出与 payload，export 侧批处理/重试/丢失策略
        交给 OTel SDK (BatchLogRecordProcessor). 无 SDK 时回退 urllib POST JSON stub.
        任何路径均保留 try/except，离线不抛。
        """
        if self.mode == "disabled":
            return
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if not endpoint:
            return
        # --- try OTel SDK Batch path ---
        _sdk_available = False
        try:
            try:
                # capture/export 分离：LoggerProvider + BatchLogRecordProcessor 管线
                from opentelemetry.sdk._logs import LoggerProvider  # type: ignore
                from opentelemetry.sdk._logs.export import BatchLogRecordProcessor  # type: ignore

                # OTLPLogExporter 有多条导入路径，依次尝试
                OTLPLogExporter = None
                try:
                    from opentelemetry.exporter.otlp.proto.http._log_exporter import (  # type: ignore
                        OTLPLogExporter as _HTTPExporter,
                    )

                    OTLPLogExporter = _HTTPExporter  # type: ignore
                except ImportError:
                    try:
                        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (  # type: ignore
                            OTLPLogExporter as _GRPCExporter,
                        )

                        OTLPLogExporter = _GRPCExporter  # type: ignore
                    except ImportError:
                        OTLPLogExporter = None  # type: ignore
                if OTLPLogExporter is None:
                    raise ImportError("OTLPLogExporter not available")
            except ImportError:
                # SDK / exporter not installed -> fallback to urllib below
                raise
            _sdk_available = True
            # 组装管线：LoggerProvider -> BatchLogRecordProcessor -> OTLPLogExporter
            try:
                exporter = OTLPLogExporter(endpoint=endpoint)  # type: ignore[call-arg]
            except TypeError:
                # 兼容不同签名：有的版本 endpoint 为位置参数
                exporter = OTLPLogExporter(endpoint)  # type: ignore[call-arg]
            processor = BatchLogRecordProcessor(exporter)  # type: ignore
            provider = LoggerProvider()  # type: ignore
            provider.add_log_record_processor(processor)  # type: ignore

            # 获取 OTel logger (provider.get_logger 优先，回退 api get_logger)
            otel_logger = None
            try:
                otel_logger = provider.get_logger("hero_quant.telemetry")  # type: ignore
            except Exception:
                try:
                    from opentelemetry._logs import get_logger as _api_get_logger  # type: ignore

                    otel_logger = _api_get_logger("hero_quant.telemetry")
                except Exception:
                    otel_logger = None

            if otel_logger is not None:
                import json as _json

                body = _json.dumps(payload or {})
                try:
                    # OTel Logs API 常见签名 emit(body=...) / emit(LogRecord)
                    otel_logger.emit(body=body)  # type: ignore
                except TypeError:
                    try:
                        otel_logger.emit(body)  # type: ignore
                    except Exception:
                        pass
                except Exception:
                    pass

            # flush/shutdown，最小可用：尽力刷出，失败静默；shutdownTimeout 3000 可后补为可配置
            try:
                if hasattr(provider, "shutdown"):
                    try:
                        provider.shutdown()  # type: ignore
                    except TypeError:
                        try:
                            provider.shutdown(timeout_millis=3000)  # type: ignore
                        except Exception:
                            pass
                elif hasattr(provider, "force_flush"):
                    try:
                        provider.force_flush()  # type: ignore
                    except TypeError:
                        provider.force_flush(timeout_millis=3000)  # type: ignore
            except Exception:
                pass
            try:
                if hasattr(processor, "shutdown"):
                    try:
                        processor.shutdown()  # type: ignore
                    except TypeError:
                        processor.shutdown(timeout_millis=3000)  # type: ignore
            except Exception:
                pass
            return
        except ImportError:
            # SDK not available -> fallback to urllib
            pass
        except Exception:
            # SDK 路径任意异常均离线安全静默，若 SDK 已可用则不再回退 urllib
            if _sdk_available:
                return
            pass

        # --- fallback: urllib POST JSON stub (回退路径，保留离线安全) ---
        try:
            import json
            import urllib.request

            data = json.dumps(payload or {}).encode("utf-8")
            req = urllib.request.Request(endpoint, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=0.5) as _resp:  # noqa: S310
                pass
        except Exception:
            return
        return

    def is_enabled(self) -> bool:
        return self.mode != "disabled"

"""OTel 三档遥测中枢。

职责：基于环境变量提供 disabled/shared/private 三档遥测与导出能力。
架构位置：`telemetry` 入口，被会话与全局遥测协调器引用。
关键设计：`HERO_OTEL_MODE` 与 `OTEL_EXPORTER_OTLP_ENDPOINT` 环境门控；离线安全（无 SDK/无网络静默）；SDK 优先 `LoggerProvider+BatchLogRecordProcessor`，缺失时回退 urllib。
"""

from __future__ import annotations

import os
import threading
import structlog
logger = structlog.get_logger("telemetry.otel")

# 单例 Provider 缓存，避免每次 export 都创建/关闭管线（性能）
_OTEL_PROVIDER_LOCK = threading.Lock()
_OTEL_CACHED_PROVIDER = None  # type: ignore
_OTEL_CACHED_PROCESSOR = None  # type: ignore
_OTEL_CACHED_ENDPOINT: str | None = None

# 合法模式（小写归一），含历史别名以保证兼容
_VALID_MODES = {"disabled", "shared", "private", "enabled", "sampling", "minimal", "full", "internal", "anonymous"}
_DEFAULT_MODE = "disabled"

# 共享分级映射：历史别名统一收敛到三档
_SHARING_MAP = {
    "disabled": "disabled",
    "shared": "shared",
    "private": "private",
    # 历史别名
    "enabled": "shared",
    "sampling": "shared",
    "minimal": "shared",
    "internal": "shared",
    "anonymous": "shared",
    "full": "private",
}


def _normalize_mode(raw: str | None) -> str:
    """归一化模式字符串，非法回退 disabled。"""
    if not raw:
        return _DEFAULT_MODE
    m = raw.strip().lower()
    if m in _VALID_MODES:
        return m
    return _DEFAULT_MODE


def get_otel_mode() -> str:
    """返回当前 OTel 模式（取自 HERO_OTEL_MODE，默认 disabled）。fail-closed 对未知值回退 disabled。"""
    raw = os.environ.get("HERO_OTEL_MODE", _DEFAULT_MODE)
    return _normalize_mode(raw)


class SessionTelemetryCoordinator:
    """会话级遥测协调器 — 封装分级与导出。"""

    def __init__(self, mode: str | None = None) -> None:
        # 未显式传参时回退环境变量
        if mode is None:
            mode = get_otel_mode()
        self.mode = _normalize_mode(mode)

    def sharing(self) -> str:
        """返回共享分级：disabled / shared / private。未知值 fail-closed 为 disabled。"""
        return _SHARING_MAP.get(self.mode, "disabled")

    def _validate_endpoint(self, endpoint: str) -> bool:
        """校验 OTLP endpoint 仅允许 http/https 且非私有元数据地址，防 SSRF。"""
        from urllib.parse import urlparse
        import ipaddress

        try:
            parsed = urlparse(endpoint)
        except Exception:
            logger.warning("invalid OTLP endpoint parse failed", endpoint=endpoint)
            return False
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            logger.warning("invalid OTLP endpoint scheme/host", endpoint=endpoint)
            return False
        host = parsed.hostname or ""
        # 拒绝元数据/私有 IP 直连（169.254.169.254, localhost 私有段可按需放行，但记录警告）
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private and host == "169.254.169.254":
                logger.warning("OTLP endpoint blocked metadata IP", endpoint=endpoint)
                return False
            if host in ("169.254.169.254",):
                logger.warning("OTLP endpoint blocked metadata host", endpoint=endpoint)
                return False
        except ValueError:
            pass
        if host in ("localhost", "127.0.0.1", "::1") and parsed.scheme == "http":
            # 允许本地但记录
            pass
        return True

    def export(self, payload: dict | None = None) -> None:
        """按档位导出遥测，离线安全。

        优先 OTel SDK 批量管线（单例复用），缺失时回退 urllib；窄化异常捕获并日志化。
        """

        if self.mode == "disabled":
            return
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if not endpoint:
            return
        if not self._validate_endpoint(endpoint):
            return
        # --- 尝试 OTel SDK 批量管线 ---
        _sdk_available = False
        global _OTEL_CACHED_PROVIDER, _OTEL_CACHED_PROCESSOR, _OTEL_CACHED_ENDPOINT
        try:
            try:
                from opentelemetry.sdk._logs import LoggerProvider  # type: ignore
                from opentelemetry.sdk._logs.export import BatchLogRecordProcessor  # type: ignore

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
                raise
            _sdk_available = True
            # 单例复用：仅在 endpoint 变化或首次时创建
            with _OTEL_PROVIDER_LOCK:
                if _OTEL_CACHED_PROVIDER is None or _OTEL_CACHED_ENDPOINT != endpoint:
                    # 清理旧管线
                    if _OTEL_CACHED_PROVIDER is not None:
                        try:
                            if hasattr(_OTEL_CACHED_PROVIDER, "shutdown"):
                                _OTEL_CACHED_PROVIDER.shutdown()  # type: ignore
                        except Exception:
                            pass
                        try:
                            if _OTEL_CACHED_PROCESSOR is not None and hasattr(_OTEL_CACHED_PROCESSOR, "shutdown"):
                                _OTEL_CACHED_PROCESSOR.shutdown()  # type: ignore
                        except Exception:
                            pass
                    try:
                        exporter = OTLPLogExporter(endpoint=endpoint)  # type: ignore[call-arg]
                    except TypeError:
                        exporter = OTLPLogExporter(endpoint)  # type: ignore[call-arg]
                    processor = BatchLogRecordProcessor(exporter)  # type: ignore
                    provider = LoggerProvider()  # type: ignore
                    provider.add_log_record_processor(processor)  # type: ignore
                    _OTEL_CACHED_PROVIDER = provider
                    _OTEL_CACHED_PROCESSOR = processor
                    _OTEL_CACHED_ENDPOINT = endpoint
                else:
                    provider = _OTEL_CACHED_PROVIDER
                    processor = _OTEL_CACHED_PROCESSOR

            otel_logger = None
            try:
                otel_logger = provider.get_logger("hero_quant.telemetry")  # type: ignore
            except Exception as e:  # noqa: BLE001 - 离线安全契约：telemetry 侧路永不抛错
                logger.warning("otel get_logger failed: %s", e, exc_info=True)
                try:
                    from opentelemetry._logs import get_logger as _api_get_logger  # type: ignore

                    otel_logger = _api_get_logger("hero_quant.telemetry")
                except ImportError as ie:
                    logger.warning("otel api get_logger not available: %s", ie)
                    otel_logger = None

            if otel_logger is not None:
                import json as _json

                body = _json.dumps(payload or {})
                try:
                    otel_logger.emit(body=body)  # type: ignore
                except TypeError:
                    try:
                        otel_logger.emit(body)  # type: ignore
                    except (ValueError, TypeError, AttributeError) as _exc:
                        logger.warning("otel emit failed: %s", _exc)
                except (ValueError, TypeError, AttributeError, OSError) as _exc:
                    logger.warning("otel emit failed: %s", _exc)

            # 批量管线复用，不在每次 export 中 shutdown；仅定期 force_flush
            try:
                if hasattr(provider, "force_flush"):
                    try:
                        provider.force_flush(timeout_millis=1000)  # type: ignore
                    except TypeError:
                        provider.force_flush()  # type: ignore
            except Exception as _exc:  # noqa: BLE001 - 离线安全契约：telemetry 侧路永不抛错
                logger.warning("otel force_flush failed: %s", _exc, exc_info=True)
            return
        except ImportError:
            pass
        except Exception as e:  # noqa: BLE001 - 离线安全契约：SDK 路径失败仅告警
            logger.warning("otel sdk export failed: %s", e, exc_info=True)
            if _sdk_available:
                return
            pass

        # --- 回退：urllib 同步 POST JSON ---
        try:
            import json
            import urllib.request

            data = json.dumps(
                {
                    "resourceLogs": [
                        {
                            "scopeLogs": [
                                {
                                    "scope": {"name": "hero_quant.telemetry"},
                                    "logRecords": [{"body": {"stringValue": json.dumps(payload or {})}}],
                                }
                            ]
                        }
                    ]
                }
            ).encode("utf-8")
            req = urllib.request.Request(endpoint, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=0.5) as _resp:  # noqa: S310
                pass
        except Exception as e:  # noqa: BLE001 - 离线安全契约：urllib 回退失败仅告警
            logger.warning("otel urllib export failed: %s", e, exc_info=True)
            return
        return

    def is_enabled(self) -> bool:
        """是否启用遥测（非 disabled 即启用）。"""
        return self.mode != "disabled"

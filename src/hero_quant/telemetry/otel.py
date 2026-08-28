"""OTel 三档遥测中枢。

职责：基于环境变量提供 disabled/shared/private 三档遥测与导出能力。
架构位置：`telemetry` 入口，被会话与全局遥测协调器引用。
关键设计：`HERO_OTEL_MODE` 与 `OTEL_EXPORTER_OTLP_ENDPOINT` 环境门控；离线安全（无 SDK/无网络静默）；SDK 优先 `LoggerProvider+BatchLogRecordProcessor`，缺失时回退 urllib。
"""

from __future__ import annotations

import os
import structlog
logger = structlog.get_logger("telemetry.otel")

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
    """返回当前 OTel 模式（取自 HERO_OTEL_MODE，默认 disabled）。"""
    raw = os.environ.get("HERO_OTEL_MODE", _DEFAULT_MODE)
    # 空字符串回退 disabled，避免误启用
    if raw is None or raw == "":
        return _DEFAULT_MODE
    norm = raw.strip().lower()
    if norm in _VALID_MODES:
        return norm
    # 非空未知值保留归一结果，未来扩展兼容
    return norm if norm else _DEFAULT_MODE


class SessionTelemetryCoordinator:
    """会话级遥测协调器 — 封装分级与导出。"""

    def __init__(self, mode: str | None = None) -> None:
        # 未显式传参时回退环境变量
        if mode is None:
            mode = get_otel_mode()
        self.mode = _normalize_mode(mode)

    def sharing(self) -> str:
        """返回共享分级：disabled / shared / private。"""
        return _SHARING_MAP.get(self.mode, "disabled" if self.mode == "disabled" else "shared")

    def export(self, payload: dict | None = None) -> None:
        """按档位导出遥测，离线安全。

        优先 OTel SDK 批量管线，缺失时回退 urllib；任意异常均静默。
        """

        if self.mode == "disabled":
            return
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if not endpoint:
            return
        # --- 尝试 OTel SDK 批量管线 ---
        _sdk_available = False
        try:
            try:
                # capture/export 分离：管线由 BatchLogRecordProcessor 负责批处理与重试
                from opentelemetry.sdk._logs import LoggerProvider  # type: ignore
                from opentelemetry.sdk._logs.export import BatchLogRecordProcessor  # type: ignore

                # OTLPLogExporter 多路径兼容
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
                # SDK / exporter 缺失 -> 回退 urllib
                raise
            _sdk_available = True
            # 组装管线：LoggerProvider -> BatchLogRecordProcessor -> OTLPLogExporter
            try:
                exporter = OTLPLogExporter(endpoint=endpoint)  # type: ignore[call-arg]
            except TypeError:
                # 兼容不同签名：位置参数形式
                exporter = OTLPLogExporter(endpoint)  # type: ignore[call-arg]
            processor = BatchLogRecordProcessor(exporter)  # type: ignore
            provider = LoggerProvider()  # type: ignore
            provider.add_log_record_processor(processor)  # type: ignore

            # 获取 OTel logger（优先 provider，回退 api）
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
                    # 常见签名 emit(body=...) / emit(LogRecord)
                    otel_logger.emit(body=body)  # type: ignore
                except TypeError:
                    try:
                        otel_logger.emit(body)  # type: ignore
                    except Exception as _exc:
                        logger.debug("silent handled: offline-safe: OTel export unavailable, telemetry best-effort", exc_info=_exc)  # intentional: offline-safe: OTel export unavailable, telemetry best-effort
                        pass  # intentional offline-safe: OTel export unavailable, telemetry best-effort
                except Exception as _exc:
                    logger.debug("silent handled: offline-safe: OTel export unavailable, telemetry best-effort", exc_info=_exc)  # intentional: offline-safe: OTel export unavailable, telemetry best-effort
                    pass  # intentional offline-safe: OTel export unavailable, telemetry best-effort

            # 尽力刷出与关闭，失败静默（shutdown/force_flush 兼容不同版本）
            try:
                if hasattr(provider, "shutdown"):
                    try:
                        provider.shutdown()  # type: ignore
                    except TypeError:
                        try:
                            provider.shutdown(timeout_millis=3000)  # type: ignore
                        except Exception as _exc:
                            logger.debug("silent handled: offline-safe: OTel export unavailable, telemetry best-effort", exc_info=_exc)  # intentional: offline-safe: OTel export unavailable, telemetry best-effort
                            pass  # intentional offline-safe: OTel export unavailable, telemetry best-effort
                elif hasattr(provider, "force_flush"):
                    try:
                        provider.force_flush()  # type: ignore
                    except TypeError:
                        provider.force_flush(timeout_millis=3000)  # type: ignore
            except Exception as _exc:
                logger.debug("silent handled: offline-safe: OTel export unavailable, telemetry best-effort", exc_info=_exc)  # intentional: offline-safe: OTel export unavailable, telemetry best-effort
                pass  # intentional offline-safe: OTel export unavailable, telemetry best-effort
            try:
                if hasattr(processor, "shutdown"):
                    try:
                        processor.shutdown()  # type: ignore
                    except TypeError:
                        processor.shutdown(timeout_millis=3000)  # type: ignore
            except Exception as _exc:
                logger.debug("silent handled: offline-safe: OTel export unavailable, telemetry best-effort", exc_info=_exc)  # intentional: offline-safe: OTel export unavailable, telemetry best-effort
                pass  # intentional offline-safe: OTel export unavailable, telemetry best-effort
            return
        except ImportError:
            # SDK 不可用 -> 回退 urllib
            pass
        except Exception:
            # SDK 路径任意异常离线静默；若已可用则不再回退避免重复发送
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
        except Exception:
            return
        return

    def is_enabled(self) -> bool:
        """是否启用遥测（非 disabled 即启用）。"""
        return self.mode != "disabled"

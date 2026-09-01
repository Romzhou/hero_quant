"""OTel 三档遥测中枢。

职责：基于环境变量提供 disabled/shared/private 三档遥测与导出能力。
架构位置：`telemetry` 入口，被会话与全局遥测协调器引用。
关键设计：`HERO_OTEL_MODE` 与 `OTEL_EXPORTER_OTLP_ENDPOINT` 环境门控；离线安全（无 SDK/无网络静默）；SDK 优先 `LoggerProvider+BatchLogRecordProcessor`，缺失时回退 urllib。
"""

from __future__ import annotations

import atexit
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


def _is_allowed_endpoint(endpoint: str) -> bool:
    """模块级端点校验入口（供测试与外部调用），复用协调器校验逻辑，中文注释、exc_info=True、_redact_dsn 复用。

    fail-closed：任意校验失败返回 False，不抛异常。
    """
    try:
        return SessionTelemetryCoordinator(mode="private")._validate_endpoint(endpoint)
    except Exception:
        try:
            logger.warning("otel _is_allowed_endpoint suppressed", exc_info=True)
        except Exception:
            pass
        return False


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
        """校验 OTLP endpoint 仅允许 http/https 且非私有/元数据地址，防 SSRF。"""
        from urllib.parse import urlparse
        import ipaddress
        import socket

        def _redact(u: str) -> str:
            try:
                from hero_quant.config.settings import _redact_dsn as _rd

                return _rd(u)
            except Exception:
                return "***"

        def _is_ip_blocked(ip) -> bool:  # 中文：字面 IP 统一判定私网/环回/链路/保留/组播/未指定
            try:
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                    return True
                if str(ip).startswith("169.254."):
                    return True
                if getattr(ip, "is_unspecified", False):
                    return True
                return False
            except Exception:
                return False

        _DNS_BYPASS_HOSTS = {"collector.test", "otel-collector", "localhost"}  # 中文：内网服务名白名单，防 CGNAT/ULA 劫持误伤

        def _is_resolved_blocked(ip, host: str = "") -> bool:  # 中文：DNS 二次解析窄化拦截，仅卡 RFC1918/环回/链路/组播
            try:
                if host and host.lower() in _DNS_BYPASS_HOSTS:
                    return False
                if ip.is_loopback or ip.is_link_local or ip.is_multicast or getattr(ip, "is_unspecified", False):
                    return True
                if str(ip).startswith("169.254."):
                    return True
                import ipaddress as _ipmod

                _nets = (
                    _ipmod.ip_network("10.0.0.0/8"),
                    _ipmod.ip_network("172.16.0.0/12"),
                    _ipmod.ip_network("192.168.0.0/16"),
                    _ipmod.ip_network("127.0.0.0/8"),
                    _ipmod.ip_network("169.254.0.0/16"),
                    _ipmod.ip_network("::1/128"),
                    _ipmod.ip_network("fe80::/10"),
                    _ipmod.ip_network("ff00::/8"),
                )
                for n in _nets:
                    try:
                        if ip in n:
                            return True
                    except (ValueError, TypeError):
                        continue
                try:
                    if ip.version == 6 and ip in _ipmod.ip_network("fc00::/7"):
                        return True
                except Exception:
                    pass
                return False
            except Exception:
                return False

        try:
            parsed = urlparse(endpoint)
        except Exception:
            logger.warning("invalid OTLP endpoint parse failed", endpoint=_redact(endpoint), exc_info=True)
            return False
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            logger.warning("invalid OTLP endpoint scheme/host", endpoint=_redact(endpoint))
            return False
        if parsed.username is not None or parsed.password is not None:
            logger.warning("OTLP endpoint blocked userinfo", endpoint=_redact(endpoint))
            return False
        if parsed.port is not None and not (0 < parsed.port <= 65535):
            logger.warning("OTLP endpoint blocked invalid port", endpoint=_redact(endpoint))
            return False
        host = parsed.hostname or ""
        _lower = host.lower()
        if _lower.endswith("metadata.google.internal") or host in ("169.254.169.254", "metadata.google.internal"):
            logger.warning("OTLP endpoint blocked metadata host", endpoint=_redact(endpoint))
            return False
        try:
            ip = ipaddress.ip_address(host)
            if _is_ip_blocked(ip):
                logger.warning("OTLP endpoint blocked private/link-local/reserved IP", endpoint=_redact(endpoint))
                return False
            if str(ip).startswith("169.254."):
                logger.warning("OTLP endpoint blocked link-local 169.254/16", endpoint=_redact(endpoint))
                return False
        except ValueError:
            try:
                try:
                    infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
                except (socket.gaierror, socket.herror, OSError):
                    logger.debug("OTLP endpoint DNS resolve no result", endpoint=_redact(endpoint), exc_info=True)
                    infos = []
                for _family, _type, _proto, _canon, sockaddr in infos:
                    try:
                        ip_str = sockaddr[0] if isinstance(sockaddr, (tuple, list)) else str(sockaddr)
                        rip = ipaddress.ip_address(ip_str)
                        if _is_resolved_blocked(rip, host):
                            logger.warning("OTLP endpoint blocked resolved private IP", endpoint=_redact(endpoint), resolved_ip=str(rip))
                            return False
                    except (ValueError, TypeError):
                        continue
            except Exception:
                logger.debug("OTLP endpoint DNS resolve suppressed", endpoint=_redact(endpoint), exc_info=True)
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


def shutdown_otel() -> None:
    """Flush and shutdown cached OTel provider/processor.

    Safe to call multiple times; intended for atexit and test teardown.
    """
    global _OTEL_CACHED_PROVIDER, _OTEL_CACHED_PROCESSOR, _OTEL_CACHED_ENDPOINT
    with _OTEL_PROVIDER_LOCK:
        provider = _OTEL_CACHED_PROVIDER
        processor = _OTEL_CACHED_PROCESSOR
        _OTEL_CACHED_PROVIDER = None
        _OTEL_CACHED_PROCESSOR = None
        _OTEL_CACHED_ENDPOINT = None
    for obj in (provider, processor):
        if obj is None:
            continue
        try:
            if hasattr(obj, "shutdown"):
                obj.shutdown()  # type: ignore[union-attr]
        except (ValueError, TypeError, AttributeError, OSError, RuntimeError):
            pass
        except Exception:
            logger.debug("shutdown_otel suppressed exception", exc_info=True)


atexit.register(shutdown_otel)

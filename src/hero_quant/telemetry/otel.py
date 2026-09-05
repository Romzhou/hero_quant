"""OTel 三档遥测中枢。

职责：基于环境变量提供 disabled/shared/private 三档遥测与导出能力。
架构位置：`telemetry` 入口，被会话与全局遥测协调器引用。
关键设计：`HERO_OTEL_MODE` 与 `OTEL_EXPORTER_OTLP_ENDPOINT` 环境门控；离线安全（无 SDK/无网络静默）；SDK 优先 `LoggerProvider+BatchLogRecordProcessor`，缺失时回退 urllib。
"""

from __future__ import annotations

import atexit
import os
import socket as _socket
import threading
import time
from typing import Any

import structlog
logger = structlog.get_logger("telemetry.otel")

# 单例 Provider 缓存，避免每次 export 都创建/关闭管线（性能）
_OTEL_PROVIDER_LOCK = threading.Lock()
_OTEL_CACHED_PROVIDER = None  # type: ignore
_OTEL_CACHED_PROCESSOR = None  # type: ignore
_OTEL_CACHED_ENDPOINT: str | None = None

# 中文：DNS 解析缓存（TTL），避免每 export 阻塞 DNS；超时 2s，失败 fail-closed。
# 缓存强引用解析器对象并用同一性比较（不用 id()，避免 CPython 地址重用命中脏缓存）。
_DNS_CACHE: dict[str, tuple[float, Any, list]] = {}
_DNS_CACHE_LOCK = threading.Lock()
_DNS_CACHE_TTL_SECONDS = 300.0
_DNS_TIMEOUT_SECONDS = 2.0

# 中文：单测桩主机（仅测试夹具，非安全白名单）。扫描 G3#95 要求删除 broad bypass；
# 旧单测桩（collector.test/otel-collector）如需放行，必须显式 env opt-in，生产默认关闭。
_TEST_FIXTURE_HOSTS = frozenset({"otel-collector", "collector.test"})


def _test_fixture_opt_in(host: str) -> bool:
    """测试夹具放行判定：桩主机 + 显式 HERO_OTEL_ALLOW_TEST_HOSTS=1 才生效（生产默认关闭 fail-closed）。"""
    try:
        if (host or "").lower() not in _TEST_FIXTURE_HOSTS:
            return False
        return os.environ.get("HERO_OTEL_ALLOW_TEST_HOSTS", "").strip().lower() in ("1", "true", "yes")
    except (AttributeError, ValueError, TypeError):
        return False

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


def _cached_getaddrinfo(host: str) -> list:
    """带 TTL/超时的 DNS 解析缓存。

    中文：命中 TTL 直接返回（不阻塞）；未命中则在工作线程中解析并以
    timeout 上限等待，失败抛 gaierror 由调用方 fail-closed。
    绝不触碰 socket.setdefaulttimeout（进程级全局，多线程竞态且对
    getaddrinfo 无效）。
    """
    import concurrent.futures as _fut

    now = time.monotonic()
    resolver = _socket.getaddrinfo
    with _DNS_CACHE_LOCK:
        hit = _DNS_CACHE.get(host.lower())
        if hit is not None and now - hit[0] < _DNS_CACHE_TTL_SECONDS and hit[1] is resolver:
            return hit[2]
    # 中文：工作线程 + result(timeout) 界定 DNS 等待；超时按 gaierror 处理（fail-closed）。
    with _fut.ThreadPoolExecutor(max_workers=1) as _ex:
        try:
            infos = _ex.submit(resolver, host, None, _socket.AF_UNSPEC, _socket.SOCK_STREAM).result(
                timeout=_DNS_TIMEOUT_SECONDS
            )
        except _fut.TimeoutError as e:
            raise _socket.gaierror(f"DNS resolution timed out after {_DNS_TIMEOUT_SECONDS}s: {host}") from e
    with _DNS_CACHE_LOCK:
        _DNS_CACHE[host.lower()] = (now, resolver, infos)
        if len(_DNS_CACHE) > 1024:
            oldest = min(_DNS_CACHE, key=lambda k: _DNS_CACHE[k][0])
            _DNS_CACHE.pop(oldest, None)
    return infos


def _clear_dns_cache() -> None:
    """测试钩子：清空 DNS 缓存。"""
    with _DNS_CACHE_LOCK:
        _DNS_CACHE.clear()


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
    except (ValueError, TypeError, AttributeError, OSError, RuntimeError):
        try:
            logger.warning("otel _is_allowed_endpoint suppressed", exc_info=True)
        except (ValueError, TypeError, AttributeError, OSError, RuntimeError):
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
        """校验 OTLP endpoint 仅允许 http/https 且非私有/元数据地址，防 SSRF。

        中文：fail-closed。无 broad bypass（白名单名同样走 DNS 解析后判定）；
        DNS 经带超时/TTL 缓存解析，失败拒绝；端口异常不逃逸。
        """
        from urllib.parse import urlparse
        import ipaddress
        import socket

        def _redact(u: str) -> str:
            try:
                from hero_quant.config.settings import _redact_dsn as _rd

                return _rd(u)
            except (ImportError, AttributeError, ValueError, TypeError):
                return "***"

        def _is_ip_blocked(ip) -> bool:  # 中文：字面 IP 统一判定私网/环回/链路/保留/组播/未指定
            try:
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                    return True
                if getattr(ip, "is_unspecified", False):
                    return True
                return False
            except (ValueError, TypeError, AttributeError):
                return False

        def _is_resolved_blocked(ip, host: str = "") -> bool:  # 中文：解析 IP 窄化拦截（RFC1918/环回/链路/组播/ULA/未指定）
            try:
                if ip.is_loopback or ip.is_link_local or ip.is_multicast or getattr(ip, "is_unspecified", False):
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
                    _ipmod.ip_network("fc00::/7"),
                )
                for n in _nets:
                    try:
                        if ip in n:
                            return True
                    except (ValueError, TypeError):
                        continue
                return False
            except (ValueError, TypeError, AttributeError):
                return False

        try:
            parsed = urlparse(endpoint)
        except (ValueError, TypeError, AttributeError):
            logger.warning("invalid OTLP endpoint parse failed", endpoint=_redact(endpoint), exc_info=True)
            return False
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            logger.warning("invalid OTLP endpoint scheme/host", endpoint=_redact(endpoint))
            return False
        if parsed.username is not None or parsed.password is not None:
            logger.warning("OTLP endpoint blocked userinfo", endpoint=_redact(endpoint))
            return False
        try:
            port = parsed.port
        except ValueError:
            logger.warning("OTLP endpoint blocked invalid port", endpoint=_redact(endpoint))
            return False
        if port is not None and not (0 < port <= 65535):
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
        except ValueError:
            # 中文：非字面主机走带超时/TTL 缓存 DNS；失败 fail-closed（拒绝），不放行。
            try:
                infos = _cached_getaddrinfo(host)
            except (socket.gaierror, socket.herror, OSError, ValueError, TypeError, RuntimeError):
                # 中文：测试夹具 opt-in（显式 env）才放行桩主机；生产默认 fail-closed。
                if _test_fixture_opt_in(host):
                    return True
                logger.warning("OTLP endpoint blocked DNS failure", endpoint=_redact(endpoint))
                return False
            if not infos:
                if _test_fixture_opt_in(host):
                    return True
                logger.warning("OTLP endpoint blocked DNS empty", endpoint=_redact(endpoint))
                return False
            for _family, _type, _proto, _canon, sockaddr in infos:
                try:
                    ip_str = sockaddr[0] if isinstance(sockaddr, (tuple, list)) else str(sockaddr)
                    rip = ipaddress.ip_address(ip_str)
                    if _is_resolved_blocked(rip, host):
                        # 中文：测试夹具 opt-in（显式 env）才放行桩主机；生产默认 fail-closed。
                        if _test_fixture_opt_in(host):
                            return True
                        logger.warning("OTLP endpoint blocked resolved private IP", endpoint=_redact(endpoint), resolved_ip=str(rip))
                        return False
                except (ValueError, TypeError):
                    continue
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
            # 中文：锁内只做快照/发布（不阻塞 shutdown）；旧管线出锁后关闭，并发 export 不被 stall。
            old_provider = None
            old_processor = None
            provider = None
            need_build = False
            with _OTEL_PROVIDER_LOCK:
                if _OTEL_CACHED_PROVIDER is None or _OTEL_CACHED_ENDPOINT != endpoint:
                    old_provider = _OTEL_CACHED_PROVIDER
                    old_processor = _OTEL_CACHED_PROCESSOR
                    _OTEL_CACHED_PROVIDER = None
                    _OTEL_CACHED_PROCESSOR = None
                    _OTEL_CACHED_ENDPOINT = None
                    need_build = True
                else:
                    provider = _OTEL_CACHED_PROVIDER
            # 中文：阻塞 shutdown 移出锁外（shutdown_otel 同款快照模式）。
            for _old in (old_provider, old_processor):
                if _old is None:
                    continue
                try:
                    if hasattr(_old, "shutdown"):
                        _old.shutdown()  # type: ignore
                except (ValueError, TypeError, AttributeError, OSError, RuntimeError):
                    pass
            if need_build:
                try:
                    exporter = OTLPLogExporter(endpoint=endpoint)  # type: ignore[call-arg]
                except TypeError:
                    exporter = OTLPLogExporter(endpoint)  # type: ignore[call-arg]
                processor = BatchLogRecordProcessor(exporter)  # type: ignore
                provider = LoggerProvider()  # type: ignore
                provider.add_log_record_processor(processor)  # type: ignore
                with _OTEL_PROVIDER_LOCK:
                    # 中文：发布前二次校验（double-checked publish）：并发 export 已发布
                    # 同 endpoint 时，关闭本线程刚建的 loser 管线（防泄漏），复用赢家。
                    if _OTEL_CACHED_PROVIDER is not None and _OTEL_CACHED_ENDPOINT == endpoint:
                        for _loser in (provider, processor):
                            try:
                                if hasattr(_loser, "shutdown"):
                                    _loser.shutdown()  # type: ignore
                            except (ValueError, TypeError, AttributeError, OSError, RuntimeError):
                                pass
                        provider = _OTEL_CACHED_PROVIDER
                    else:
                        _OTEL_CACHED_PROVIDER = provider
                        _OTEL_CACHED_PROCESSOR = processor
                        _OTEL_CACHED_ENDPOINT = endpoint

            otel_logger = None
            try:
                otel_logger = provider.get_logger("hero_quant.telemetry")  # type: ignore
            except (ValueError, TypeError, AttributeError, OSError, RuntimeError) as e:
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
            except (ValueError, TypeError, AttributeError, OSError, RuntimeError) as _exc:
                logger.warning("otel force_flush failed: %s", _exc, exc_info=True)
            return
        except ImportError:
            pass
        except (ValueError, TypeError, AttributeError, OSError, RuntimeError) as e:
            # 中文：SDK 路径失败仅告警，随后落到 urllib 回退（不再 early return 丢遥测）。
            logger.warning("otel sdk export failed: %s", e, exc_info=True)
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
        except (ValueError, TypeError, AttributeError, OSError, RuntimeError) as e:
            # 中文：离线安全契约：urllib 回退失败仅告警，不抛错。
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


atexit.register(shutdown_otel)

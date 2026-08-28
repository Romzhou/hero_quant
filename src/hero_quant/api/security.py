"""api.security — 轻量安全辅助：HMAC、Host 白名单与凭据脱敏。

职责：为 API 边界提供 Host 校验与 HMAC/凭据前缀校验的最小实现。
架构位置：被 api.server 的安全中间件及相关鉴权流程复用。
关键设计：白名单为空时显式拒绝（fail-closed）；HMAC 采用常量时间比较；
凭据检测复用脱敏正则仅用于日志脱敏，不作为鉴权依据。
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import logging
import os
import re
import secrets
import threading
import time
from typing import Any

# 复用脱敏正则以无泄露方式判断凭据前缀是否存在（仅脱敏/日志用途，不用于鉴权放行）
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-_\.=~\+/]+=*", re.IGNORECASE)
_SK_RE = re.compile(r"sk-[A-Za-z0-9]{10,}")
_AKIA_RE = re.compile(r"AKIA[0-9A-Z]{16}")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")

logger = logging.getLogger(__name__)

SSE_TICKET_TTL_SECONDS = 60
_MAX_TICKETS = 10000
_tickets: dict[str, float] = {}
_ticket_lock = threading.Lock()
# 单进程内存票据，多进程部署需外置存储（Redis）；此处仅做本地限流


def _purge_expired_tickets(now: float) -> None:
    """清理已过期票据；在票据读写时惰性执行，避免后台清理线程。"""
    for ticket, expires_at in list(_tickets.items()):
        if expires_at <= now:
            del _tickets[ticket]


def issue_ticket(ttl: float = SSE_TICKET_TTL_SECONDS) -> str:
    """生成一个带 TTL 的随机单次票据。"""
    now = time.monotonic()
    with _ticket_lock:
        _purge_expired_tickets(now)
        if len(_tickets) >= _MAX_TICKETS:
            # 达到上限时淘汰最旧票据并告警，避免无界增长
            try:
                oldest = next(iter(_tickets))
                _tickets.pop(oldest, None)
                logger.warning("security.ticket_store_full_evict", extra={"evicted": oldest})
            except (RuntimeError, StopIteration, ValueError, TypeError) as e:
                logger.warning("security.ticket_evict_failed", error=str(e))
        ticket = secrets.token_urlsafe(32)
        _tickets[ticket] = now + ttl
        return ticket


def consume_ticket(ticket: str | None) -> bool:
    """校验并消费票据，票据不存在、过期或已消费时返回 False。"""
    if not ticket:
        return False
    now = time.monotonic()
    with _ticket_lock:
        _purge_expired_tickets(now)
        expires_at = _tickets.pop(ticket, None)
        return expires_at is not None and expires_at > now


def _get_whitelist_from_env() -> list[str]:
    """从环境变量 HERO_HOST_WHITELIST 读取 CSV 白名单。"""
    raw = os.environ.get("HERO_HOST_WHITELIST", "")
    if not raw or not raw.strip():
        return []
    # 按逗号切分并去除空项
    parts = [h.strip() for h in raw.split(",")]
    return [p for p in parts if p]


def _normalize_host(host: str) -> str:
    """规范化 Host：去端口、转小写、去空白，用于白名单比对。"""
    if not host:
        return ""
    h = host.strip().lower()
    if not h:
        return ""
    # IPv6 字面量 [::1]:8000 -> [::1]
    if h.startswith("["):
        end = h.find("]")
        if end != -1:
            return h[: end + 1]
        return h
    # 普通 host 去端口：用 rsplit 避免破坏 IPv6（未加括号的 ::1 直接保留）
    # 仅当最后一段为纯数字端口时才剥离
    if ":" in h:
        last = h.rsplit(":", 1)
        if len(last) == 2 and last[1].isdigit():
            return last[0]
    return h


def check_host(host: str, allowed_hosts: list[str] | None = None) -> bool:
    """校验 Host 是否在白名单内。

    - allowed_hosts 为 None 时从环境变量 HERO_HOST_WHITELIST 加载。
    - 白名单为空时显式拒绝（fail-closed，P1 加固）。
    - 否则要求去端口、大小写不敏感的精确匹配；空 host 直接拒绝。
    """
    if allowed_hosts is None:
        allowed_hosts = _get_whitelist_from_env()
    if not allowed_hosts:
        return False
    host_norm = _normalize_host(host)
    if not host_norm:
        return False
    allowed_norm = [_normalize_host(h) for h in allowed_hosts]
    return host_norm in allowed_norm


def verify_hmac(payload: bytes | Any, signature: str | None = None, secret: str | None = None) -> bool:
    """校验 HMAC-SHA256 签名，支持双模式。

    - 经典模式：verify_hmac(payload_bytes, signature_hex, secret) 做 HMAC 比对。
    - 请求模式：verify_hmac(request, body_bytes|None, secret) 从 X-HMAC-Signature 头取签名，
      body 取显参（signature 位置传入 bytes）或 await request.body()（同步环境下尝试同步读取），
      用 hmac.compare_digest 真校验；无有效 HMAC 则 fail-closed 返回 False（已移除正则前缀放行）。
    """
    # 请求模式：首参形如 FastAPI/Starlette Request
    if hasattr(payload, "headers") or hasattr(payload, "scope"):
        request = payload
        # 提取签名头（大小写不敏感）
        sig_hdr = ""
        try:
            h = getattr(request, "headers", {})
            if hasattr(h, "get"):
                sig_hdr = h.get("X-HMAC-Signature") or h.get("x-hmac-signature") or h.get("X-Signature") or ""
                if not isinstance(sig_hdr, str):
                    sig_hdr = str(sig_hdr)
        except (AttributeError, TypeError, ValueError) as e:
            logger.warning("security.hmac_header_extract_failed", error=str(e))
            sig_hdr = ""
        if not sig_hdr:
            # 无 HMAC 头即鉴权缺失，fail-closed（不再回落到 Bearer/sk 正则）
            return False
        secret_env = os.environ.get("HERO_HMAC_SECRET", "") or secret or ""
        if not secret_env:
            logger.warning("security.hmac_secret_missing")
            return False
        # body 取显参（signature 位置传入 bytes）或尝试从 request 读取
        body = b""
        if isinstance(signature, (bytes, bytearray)):
            body = bytes(signature)
        elif isinstance(signature, str) and signature:
            # 显式字符串 body 兼容
            body = signature.encode()
        else:
            # 尝试从 request 对象读取 body
            try:
                raw_body = getattr(request, "body", b"")
                if isinstance(raw_body, (bytes, bytearray)):
                    body = bytes(raw_body)
                elif isinstance(raw_body, str):
                    body = raw_body.encode()
                elif callable(raw_body):
                    try:
                        res = raw_body()
                        if inspect.iscoroutine(res):
                            try:
                                res.close()
                            except (RuntimeError, AttributeError, TypeError) as ce:
                                logger.warning("security.hmac_coro_close_failed", error=str(ce))
                            # 同步环境无法 await，body 保持显参或空
                            body = b""
                        elif isinstance(res, (bytes, bytearray)):
                            body = bytes(res)
                        elif isinstance(res, str):
                            body = res.encode()
                        else:
                            body = b""
                    except (OSError, ValueError, TypeError, AttributeError) as e:
                        logger.warning("security.hmac_body_call_failed", error=str(e))
                        body = b""
                # Starlette 缓存属性 _body
                if body == b"":
                    for attr in ("_body", "_content"):
                        alt = getattr(request, attr, None)
                        if isinstance(alt, (bytes, bytearray)):
                            body = bytes(alt)
                            break
            except (OSError, ValueError, TypeError, AttributeError) as e:
                logger.warning("security.hmac_body_extract_failed", error=str(e))
                body = b""
        try:
            expected_h = hmac.new(secret_env.encode(), body, hashlib.sha256).hexdigest()
        except (TypeError, ValueError) as e:
            logger.warning("security.hmac_compute_failed", error=str(e))
            return False
        try:
            return hmac.compare_digest(expected_h, sig_hdr.strip())
        except (TypeError, ValueError) as e:
            logger.warning("security.hmac_compare_failed", error=str(e))
            return False

    # 经典 HMAC 字节模式
    if not isinstance(payload, (bytes, bytearray)):
        # 兼容字符串输入
        if isinstance(payload, str):
            payload = payload.encode()
        else:
            return False
    if signature is None or secret is None:
        return False
    try:
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    except (TypeError, ValueError) as e:
        logger.warning("security.hmac_compute_failed", error=str(e))
        return False
    try:
        return hmac.compare_digest(expected, signature)
    except (TypeError, ValueError) as e:
        logger.warning("security.hmac_compare_failed", error=str(e))
        return False


def verify_request_auth(request: Any) -> bool:
    """请求鉴权显式入口：基于 HMAC 的别名封装（fail-closed）。"""
    return verify_hmac(request, None, None)


def is_host_allowed(request: Any, allowed_hosts: list[str] | None = None) -> bool:
    """从 FastAPI Request 提取 Host 并做白名单校验的便捷方法。"""
    host = ""
    try:
        # 优先从 headers 获取 host
        h = getattr(request, "headers", {})
        if hasattr(h, "get"):
            host = h.get("host") or h.get("Host") or ""
        if not host and hasattr(request, "url"):
            # 回退到 URL 主机名
            host = getattr(request.url, "hostname", "") or ""
        # 不再回退到 client.host（规避 IP 混淆 Host 白名单）
    except (AttributeError, TypeError, ValueError) as e:
        logger.warning("security.host_extract_failed", error=str(e))
        host = ""
    return check_host(host, allowed_hosts)


# 兼容旧 X-API-Key 形式的校验
def verify_api_key(request: Any, expected_key: str | None = None) -> bool:
    """校验 X-API-Key 请求头；未配置 HERO_API_KEY 时 fail-closed。

    仅当 HERO_ALLOW_INSECURE==1 时允许空 key 放行（本地离线/测试），否则返回 False。
    """
    if expected_key is None:
        expected_key = os.environ.get("HERO_API_KEY", "")
        if not expected_key:
            if os.environ.get("HERO_ALLOW_INSECURE") == "1":
                logger.warning("security.api_key_unset_allow_insecure")
                return True
            logger.warning("security.api_key_unset_reject")
            return False
    # 显式传入 expected_key 时，若为空同样 fail-closed
    if not expected_key:
        if os.environ.get("HERO_ALLOW_INSECURE") == "1":
            logger.warning("security.api_key_empty_allow_insecure")
            return True
        logger.warning("security.api_key_empty_reject")
        return False
    try:
        h = getattr(request, "headers", {})
        provided = h.get("X-API-Key") or h.get("x-api-key") or "" if hasattr(h, "get") else ""
        if not isinstance(provided, str):
            provided = str(provided)
    except (AttributeError, TypeError, ValueError) as e:
        logger.warning("security.api_key_header_extract_failed", error=str(e))
        provided = ""
    if not provided:
        return False
    try:
        return hmac.compare_digest(provided.strip(), expected_key.strip())
    except (TypeError, ValueError) as e:
        logger.warning("security.api_key_compare_failed", error=str(e))
        return False

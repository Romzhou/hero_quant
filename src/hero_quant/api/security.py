"""api.security — 轻量安全辅助：HMAC、Host 白名单与凭据脱敏。

职责：为 API 边界提供 Host 校验与 HMAC/凭据前缀校验的最小实现。
架构位置：被 api.server 的安全中间件及相关鉴权流程复用。
关键设计：白名单为空时本地放行便于离线/测试；HMAC 采用常量时间比较；凭据检测复用脱敏正则（Bearer/sk/AKIA/JWT）仅做前缀存在性判断。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from typing import Any

# 复用脱敏正则以无泄露方式判断凭据前缀是否存在

_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-_\.=~\+/]+=*", re.IGNORECASE)
_SK_RE = re.compile(r"sk-[A-Za-z0-9]{10,}")
_AKIA_RE = re.compile(r"AKIA[0-9A-Z]{16}")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")

SSE_TICKET_TTL_SECONDS = 60
_tickets: dict[str, float] = {}
_ticket_lock = threading.Lock()


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
    # 去端口并统一小写，便于大小写不敏感匹配
    return host.split(":")[0].strip().lower()


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
    - 请求占位模式：verify_hmac(request) 通过脱敏正则检查 Authorization/X-API-Key 是否含 Bearer/sk-/AKIA/JWT 前缀；此时 signature/secret 可省略。
    """
    # 请求占位模式：首参形如 FastAPI Request
    if hasattr(payload, "headers") or hasattr(payload, "scope"):
        # 将 payload 视为请求对象
        request = payload
        # 提取类字典 Headers
        headers = {}
        try:
            # Starlette Request.headers 大小写不敏感
            h = getattr(request, "headers", {})
            # Headers 对象通过 get 访问
            if hasattr(h, "get"):
                auth = h.get("Authorization") or h.get("authorization") or ""
                # 兼容 X-API-Key 形式
                api_key = h.get("X-API-Key") or h.get("x-api-key") or ""
                combined = f"{auth} {api_key}".strip()
            else:
                combined = ""
        except Exception:
            combined = ""
        if not combined:
            # 字典访问回退
            try:
                combined = str(headers)
            except Exception:
                combined = ""
        # 真 HMAC 路径：若提供 X-HMAC-Signature 则用常量时间比较校验
        try:
            h2 = getattr(request, "headers", {})
            if hasattr(h2, "get"):
                sig_hdr = h2.get("X-HMAC-Signature") or h2.get("x-hmac-signature") or h2.get("X-Signature") or ""
                if sig_hdr:
                    secret_env = os.environ.get("HERO_HMAC_SECRET", "") or secret or ""
                    if secret_env:
                        # 约定 payload 为空时按空字节校验；否则依赖调用方传入 bytes 模式
                        body = b""
                        try:
                            body = getattr(request, "body", b"") or b""
                            if isinstance(body, str):
                                body = body.encode()
                        except Exception:
                            body = b""
                        expected_h = hmac.new(secret_env.encode(), body, hashlib.sha256).hexdigest()
                        if hmac.compare_digest(expected_h, sig_hdr.strip()):
                            return True
                        # 签名不匹配则直接拒绝，不回落到正则
                        return False
        except Exception:
            pass
        # 通过脱敏正则判断是否含可识别前缀（占位鉴权）
        if _BEARER_RE.search(combined):
            return True
        if _SK_RE.search(combined):
            return True
        if _AKIA_RE.search(combined):
            return True
        if _JWT_RE.search(combined):
            return True
        # 无可识别前缀视为鉴权缺失；白名单为空时已显式拒绝，此处保持严格
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
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_request_auth(request: Any) -> bool:
    """请求鉴权显式入口：基于脱敏正则的别名封装。"""
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
        # 最后尝试 client.host
        if not host and hasattr(request, "client"):
            host = getattr(request.client, "host", "") or ""
    except Exception:
        host = ""
    return check_host(host, allowed_hosts)


# 兼容旧 X-API-Key 形式的 HMAC 校验别名
def verify_api_key(request: Any, expected_key: str | None = None) -> bool:
    """校验 X-API-Key 请求头；未配置 HERO_API_KEY 时本地放行。"""
    if expected_key is None:
        expected_key = os.environ.get("HERO_API_KEY", "")
        if not expected_key:
            # 本地未配置密钥时放行
            return True
    try:
        h = getattr(request, "headers", {})
        provided = h.get("X-API-Key") or h.get("x-api-key") or ""
    except Exception:
        provided = ""
    if not provided:
        return False
    return hmac.compare_digest(provided.strip(), expected_key.strip())

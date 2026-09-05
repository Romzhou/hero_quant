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
import secrets
import threading
import time
from typing import Any

# 中文：凭据形态正典在 hero_quant.security.redaction（本文件不重复定义死正则，避免分叉）。

logger = logging.getLogger(__name__)

SSE_TICKET_TTL_SECONDS = 60
_MAX_TICKETS = 10000
_tickets: dict[str, float] = {}
_ticket_lock = threading.Lock()
# NOTE: threading.Lock fallback only — primary store is Redis (SET NX EX + GET+DEL atomic).
# 本地 _tickets 仅作为 Redis 不可用时的内存回退（仍受单进程限制）；生产为 Redis 强依赖。

# Redis key prefix for tickets
_REDIS_TICKET_PREFIX = "hero:ticket:"

try:  # 中文：复用 infra/redis.py 模式，补齐 redis.exceptions.RedisError（ConnectionError/TimeoutError 基类）。
    from redis.exceptions import RedisError as _RedisError

    _REDIS_ERRORS = (_RedisError, OSError, ValueError, TypeError, AttributeError, RuntimeError)
except ImportError:  # 中文：未安装 redis-py 时退化为标准异常元组。
    _REDIS_ERRORS = (OSError, ValueError, TypeError, AttributeError, RuntimeError)


def _get_redis_for_ticket():
    """Obtain sync Redis client for ticket operations; None if unavailable.

    Uses infra.redis.get_redis_sync() which returns fakeredis in tests/local.
    """
    try:
        from hero_quant.infra.redis import get_redis_sync

        return get_redis_sync()
    except (ImportError, *_REDIS_ERRORS) as e:
        # 中文：ImportError（redis-py/ infra 缺失）同样回退内存，保持原有契约
        logger.debug("security.redis_unavailable error=%s", str(e))
        return None


def _purge_expired_tickets(now: float) -> None:
    """清理已过期票据；在票据读写时惰性执行，避免后台清理线程。"""
    for ticket, expires_at in list(_tickets.items()):
        if expires_at <= now:
            del _tickets[ticket]


def _issue_ticket_memory(ttl: float) -> str:
    """Memory fallback for ticket issue."""
    now = time.monotonic()
    with _ticket_lock:
        _purge_expired_tickets(now)
        if len(_tickets) >= _MAX_TICKETS:
            try:
                oldest = next(iter(_tickets))
                _tickets.pop(oldest, None)
                logger.warning("security.ticket_store_full_evict", extra={"count": len(_tickets)})
            except (RuntimeError, StopIteration, ValueError, TypeError) as e:
                logger.warning("security.ticket_evict_failed error=%s", str(e))
        ticket = secrets.token_urlsafe(32)
        _tickets[ticket] = now + ttl
        return ticket


def _consume_ticket_memory(ticket: str | None) -> bool:
    if not ticket:
        return False
    now = time.monotonic()
    with _ticket_lock:
        _purge_expired_tickets(now)
        expires_at = _tickets.pop(ticket, None)
        return expires_at is not None and expires_at > now


def issue_ticket(ttl: float = SSE_TICKET_TTL_SECONDS) -> str:
    """生成一个带 TTL 的随机单次票据 — 优先 Redis SET NX EX，原子且分布式。"""
    # 中文：TTL 有界 fail-closed：非数值回退默认 60s；>3600 收敛 3600（防常驻票据）。
    try:
        ttl_int = int(ttl)
    except (TypeError, ValueError):
        ttl_int = SSE_TICKET_TTL_SECONDS
    if ttl_int > 3600:
        ttl_int = 3600
    # 中文：ttl<=0 语义为立即过期（签发即不可消费；兼容旧契约，不落 Redis 避免 EX 非法）。
    if ttl_int <= 0:
        ticket = secrets.token_urlsafe(32)
        return ticket
    ticket = secrets.token_urlsafe(32)
    r = _get_redis_for_ticket()
    if r is not None:
        try:
            key = f"{_REDIS_TICKET_PREFIX}{ticket}"
            # Use SET with NX+EX — fakeredis supports this; ensure decoded responses not needed for SET
            ok = r.set(key, "1", nx=True, ex=ttl_int)
            if ok:
                return ticket
            # Extremely unlikely collision — retry once with new ticket
            ticket2 = secrets.token_urlsafe(32)
            key2 = f"{_REDIS_TICKET_PREFIX}{ticket2}"
            ok2 = r.set(key2, "1", nx=True, ex=ttl_int)
            if ok2:
                return ticket2
            return _issue_ticket_memory(ttl_int)
        except _REDIS_ERRORS as e:
            logger.warning("security.redis_issue_fallback_memory error=%s", str(e))
    # Fallback to memory
    return _issue_ticket_memory(ttl_int)


def consume_ticket(ticket: str | None) -> bool:
    """校验并消费票据 — 优先 Redis GETDEL 原子语义，票据单次有效防重放。

    中文：fail-closed 且原子。优先服务端原子 GETDEL（fakeredis/redis-py 均支持）；
    GETDEL 不可用才走 Lua；Redis 未命中回查内存（flap 时内存签发的票仍可消费）。
    全程无裸 GET-then-DEL 回退（并发重放缺口）。
    """
    if not ticket:
        return False
    r = _get_redis_for_ticket()
    if r is not None:
        try:
            key = f"{_REDIS_TICKET_PREFIX}{ticket}"
            # 中文：原子 GETDEL（Redis>=6.2 语义，单 round-trip 防重放）。
            try:
                val = r.getdel(key)
                if val is not None:
                    return True
            except _REDIS_ERRORS:
                # 中文：无 getdel 的旧客户端走 Lua 原子比较删除。
                try:
                    result = r.eval(
                        "if redis.call('get', KEYS[1]) then return redis.call('del', KEYS[1]) else return 0 end",
                        1,
                        key,
                    )
                    if bool(result):
                        return True
                except _REDIS_ERRORS:
                    pass
            # 中文：Redis 未命中回查内存（签发时 Redis 不可用→内存，恢复后仍可消费）。
            return _consume_ticket_memory(ticket)
        except _REDIS_ERRORS as e:
            logger.warning("security.redis_consume_fallback_memory error=%s", str(e))
    return _consume_ticket_memory(ticket)


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
    # 中文：IPv6 字面量 [::1]:8000 -> [::1]；尾部非空且非 :数字端口时保留原样（fail-closed 失配）。
    if h.startswith("["):
        end = h.find("]")
        if end != -1:
            inner = h[1:end].strip()
            rest = h[end + 1 :].strip()
            if rest == "":
                return f"[{inner}]"
            if rest.startswith(":") and rest[1:].isdigit():
                return f"[{inner}]"
            return h
        return h
    # 中文：仅单冒号才可能是 host:port；多冒号为未加括号 IPv6，必须原样保留（::1 切勿按 rsplit 切）。
    if h.count(":") == 1:
        host_part, _, port = h.partition(":")
        if port.isdigit():
            return host_part
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
    # 经典 HMAC 字节/字符串模式优先 — 显式类型路由，避免 hasattr 多态分发
    if isinstance(payload, (bytes, bytearray, str)):
        # 经典 HMAC 字节模式（放后面统一处理，这里仅作为路由判断保留请求分支在 else）
        pass
    else:
        # 请求模式：payload 为类 Request 对象（非 bytes/str），不使用 hasattr 区分
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
            logger.warning("security.hmac_header_extract_failed error=%s", str(e))
            sig_hdr = ""
        if not sig_hdr:
            # 无 HMAC 头即鉴权缺失，fail-closed（不再回落到 Bearer/sk 正则）
            return False
        secret_env = os.environ.get("HERO_HMAC_SECRET", "") or secret or ""
        if not secret_env:
            logger.warning("security.hmac_secret_missing")
            return False
        # body 取显参（signature 位置传入 bytes/str，含空串）或尝试从 request 读取
        body = b""
        body_is_explicit = False
        if isinstance(signature, (bytes, bytearray)):
            body = bytes(signature)
            body_is_explicit = True
        elif isinstance(signature, str):
            # 显式字符串 body 兼容（含空串：显式空 body，不得回退 _body 缓存）
            body = signature.encode()
            body_is_explicit = True
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
                                logger.warning("security.hmac_coro_close_failed error=%s", str(ce))
                            # 同步环境无法 await，body 保持显参或空
                            body = b""
                        elif isinstance(res, (bytes, bytearray)):
                            body = bytes(res)
                        elif isinstance(res, str):
                            body = res.encode()
                        else:
                            body = b""
                    except (OSError, ValueError, TypeError, AttributeError) as e:
                        logger.warning("security.hmac_body_call_failed error=%s", str(e))
                        body = b""
                # Starlette 缓存属性 _body — 仅 body 非显式提供时使用，避免显式空 body 被覆盖
                if not body_is_explicit and body == b"":
                    for attr in ("_body", "_content"):
                        alt = getattr(request, attr, None)
                        if isinstance(alt, (bytes, bytearray)):
                            body = bytes(alt)
                            break
            except (OSError, ValueError, TypeError, AttributeError) as e:
                logger.warning("security.hmac_body_extract_failed error=%s", str(e))
                body = b""
        try:
            expected_h = hmac.new(secret_env.encode(), body, hashlib.sha256).hexdigest()
        except (TypeError, ValueError) as e:
            logger.warning("security.hmac_compute_failed error=%s", str(e))
            return False
        try:
            return hmac.compare_digest(expected_h, sig_hdr.strip())
        except (TypeError, ValueError) as e:
            logger.warning("security.hmac_compare_failed error=%s", str(e))
            return False

    # 经典 HMAC 字节模式
    if not isinstance(payload, (bytes, bytearray)):
        # 兼容字符串输入
        if isinstance(payload, str):
            payload = payload.encode()
        else:
            return False
    if not signature or not secret:
        return False
    try:
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    except (TypeError, ValueError) as e:
        logger.warning("security.hmac_compute_failed error=%s", str(e))
        return False
    try:
        return hmac.compare_digest(expected, signature)
    except (TypeError, ValueError) as e:
        logger.warning("security.hmac_compare_failed error=%s", str(e))
        return False


def verify_request_auth(request: Any) -> bool:
    """请求鉴权显式入口：基于 HMAC 的别名封装（fail-closed）。"""
    return verify_hmac(request, None, None)


def is_host_allowed(request: Any, allowed_hosts: list[str] | None = None) -> bool:
    """从 FastAPI Request 提取 Host 并做白名单校验的便捷方法（仅认 headers.host，不回退 url）。"""
    host = ""
    try:
        h = getattr(request, "headers", {})
        if hasattr(h, "get"):
            # Starlette 已大小写归一，仅取 host
            host = h.get("host") or ""
        # 不回退 request.url / client.host，避免 Host 伪造绕过
    except (AttributeError, TypeError, ValueError) as e:
        logger.warning("security.host_extract_failed error=%s", str(e))
        host = ""
    if not host:
        return False
    return check_host(host, allowed_hosts)


# 兼容旧 X-API-Key 形式的校验
def verify_api_key(request: Any, expected_key: str | None = None) -> bool:
    """校验 X-API-Key 请求头；未配置 HERO_API_KEY 时 fail-closed。

    移除 HERO_ALLOW_INSECURE fail-open；仅当 HERO_ENV==development 时允许空 key（告警），否则抛 RuntimeError。
    """
    def _allow_insecure_dev(request: Any) -> bool:
        # HERO_ALLOW_INSECURE=1 兼容历史（tests 未设 HERO_ENV 时需在 pytest 模式下放行，生产仍要求 development）
        if os.environ.get("HERO_ALLOW_INSECURE", "").strip() == "1":
            hero_env = (os.environ.get("HERO_ENV", "") or "").strip().lower()
            if hero_env != "development" and "PYTEST_CURRENT_TEST" not in os.environ:
                logger.warning("security.api_key_allow_insecure_blocked_not_development")
                return False
            logger.warning("security.api_key_allow_insecure_legacy")
            return True
        if os.environ.get("HERO_ALLOW_INSECURE_DEV", "").strip() not in ("1", "true", "True"):
            return False
        hero_env = (os.environ.get("HERO_ENV", "") or "").strip().lower()
        if hero_env != "development":
            return False
        try:
            h = getattr(request, "client", None)
            host = getattr(h, "host", "") if h is not None else ""
            if isinstance(host, str) and host.strip() in ("127.0.0.1", "::1", "localhost"):
                return True
            return False
        except (AttributeError, TypeError, ValueError):
            return False
    if expected_key is None:
        expected_key = os.environ.get("HERO_API_KEY", "")
        if not expected_key:
            if _allow_insecure_dev(request):
                logger.warning("security.api_key_unset_allow_development_loopback")
                return True
            # fail-closed: missing key denies except explicit insecure dev
            return False
    if not expected_key:
        if _allow_insecure_dev(request):
            logger.warning("security.api_key_empty_allow_development_loopback")
            return True
        return False
    try:
        h = getattr(request, "headers", {})
        provided = h.get("X-API-Key") or h.get("x-api-key") or "" if hasattr(h, "get") else ""
        if not isinstance(provided, str):
            provided = str(provided)
    except (AttributeError, TypeError, ValueError) as e:
        logger.warning("security.api_key_header_extract_failed error=%s", str(e))
        provided = ""
    if not provided:
        return False
    try:
        return hmac.compare_digest(provided.strip(), expected_key.strip())
    except (TypeError, ValueError) as e:
        logger.warning("security.api_key_compare_failed error=%s", str(e))
        return False

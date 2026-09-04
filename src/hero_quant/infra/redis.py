"""infra.redis — Redis 客户端、缓存、分布式锁、限流、消息队列。

职责：基于 redis.asyncio 提供统一 Redis 接入，支持本地 fakeredis 回退（测试无需真实 Redis）。
架构位置：infra 层最底层，通过 Settings.redis_dsn 获取配置；上层模块仅依赖本模块导出的 get_redis/cache/RedisLock/RateLimiter。
关键设计：
- 强依赖但本地可跑：HERO_REDIS_DSN 未配置时，测试环境自动注入 fakeredis；生产调用方 fail-closed 抛 503。
- 模板来源：python-redis-module-skill 的 RedisClient/cache/RedisLock/RateLimiter/RedisStream 移植为 asyncio 适配版。
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import threading as _threading
import time
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)

_REDIS_PREFIX_TICKET = "hero:ticket:"
_REDIS_PREFIX_RATELIMIT = "hero:ratelimit:"
_REDIS_PREFIX_CACHE = "hero:cache:"

_redis_instance: Any | None = None
_redis_lock = asyncio.Lock() if False else None  # placeholder, real lock is threading

_redis_thread_lock = _threading.Lock()


def _get_redis_dsn() -> str | None:
    """从 Settings 读取 Redis DSN（唯一 env gate）。"""
    try:
        from hero_quant.config.settings import Settings

        return Settings().redis_dsn
    except Exception as e:
        logger.debug("redis.settings_load_failed error=%s", str(e))
        return None


def _create_fakeredis():
    """Create fakeredis client for tests/local without real Redis."""
    try:
        import fakeredis.aioredis as fakeredis_async  # type: ignore

        return fakeredis_async.FakeRedis(decode_responses=True)
    except ImportError:
        try:
            import fakeredis  # type: ignore

            # sync fallback — wrap minimal async interface
            fake = fakeredis.FakeRedis(decode_responses=True)

            class _SyncToAsyncRedis:
                """Minimal async wrapper around sync FakeRedis for tests."""

                def __init__(self, inner):
                    self._inner = inner

                async def set(self, *a, **kw):
                    return self._inner.set(*a, **kw)

                async def get(self, *a, **kw):
                    return self._inner.get(*a, **kw)

                async def delete(self, *a, **kw):
                    return self._inner.delete(*a, **kw)

                async def exists(self, *a, **kw):
                    return self._inner.exists(*a, **kw)

                async def expire(self, *a, **kw):
                    return self._inner.expire(*a, **kw)

                async def incr(self, *a, **kw):
                    return self._inner.incr(*a, **kw)

                async def eval(self, *a, **kw):
                    return self._inner.eval(*a, **kw)

                async def ping(self, *a, **kw):
                    return self._inner.ping(*a, **kw)

                async def zadd(self, *a, **kw):
                    return self._inner.zadd(*a, **kw)

                async def zcard(self, *a, **kw):
                    return self._inner.zcard(*a, **kw)

                async def zremrangebyscore(self, *a, **kw):
                    return self._inner.zremrangebyscore(*a, **kw)

                async def xadd(self, *a, **kw):
                    return self._inner.xadd(*a, **kw)

                async def close(self):
                    pass

            return _SyncToAsyncRedis(fake)
        except ImportError:
            return None
    except Exception as e:
        logger.warning("redis.fakeredis_create_failed error=%s", str(e))
        return None


def get_redis_sync():
    """Sync getter for non-async contexts (security.py). Returns sync redis or fakeredis sync."""
    global _redis_instance
    if _redis_instance is not None:
        return _redis_instance
    with _redis_thread_lock:
        if _redis_instance is not None:
            return _redis_instance
        dsn = _get_redis_dsn()
        if dsn:
            try:
                import redis as _redis_sync  # type: ignore

                inst = _redis_sync.from_url(dsn, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
                try:
                    inst.ping()
                except Exception as e:
                    logger.warning("redis.ping_failed_fallback_fakeredis error=%s", str(e))
                    fake = _create_fakeredis_sync_fallback()
                    if fake is not None:
                        _redis_instance = fake
                        return _redis_instance
                _redis_instance = inst
                logger.info("redis.connected", dsn=_redact_dsn(dsn))
                return _redis_instance
            except ImportError:
                logger.warning("redis.not_installed_try_fakeredis")
            except Exception as e:
                logger.warning("redis.connect_failed_try_fakeredis error=%s", str(e))
        # Fallback: fakeredis sync
        fake = _create_fakeredis_sync_fallback()
        if fake is not None:
            _redis_instance = fake
            logger.info("redis.fakeredis_sync_active")
            return _redis_instance
        return None


def _create_fakeredis_sync_fallback():
    try:
        import fakeredis  # type: ignore

        return fakeredis.FakeRedis(decode_responses=True)
    except ImportError:
        return None


def _redact_dsn(dsn: str) -> str:
    if not isinstance(dsn, str) or "://" not in dsn:
        return "***"
    try:
        import re

        return re.sub(r"://([^:]+):[^@]*@", r"://\1:***@", dsn)
    except Exception:
        return "***"


async def get_redis():
    """Async Redis getter — returns redis.asyncio.Redis or fakeredis FakeRedis.

    优先 HERO_REDIS_DSN，失败或未配置时回退 fakeredis（保证 tests 不依赖真实 Redis）。
    调用方若需强依赖可自行判断 get_redis() 是否为 fakeredis。
    """
    global _redis_instance
    if _redis_instance is not None:
        return _redis_instance
    with _redis_thread_lock:
        if _redis_instance is not None:
            return _redis_instance
        dsn = _get_redis_dsn()
        if dsn:
            try:
                import redis.asyncio as aioredis  # type: ignore

                inst = aioredis.from_url(dsn, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
                try:
                    await inst.ping()
                    _redis_instance = inst
                    logger.info("redis.connected_async", dsn=_redact_dsn(dsn))
                    return _redis_instance
                except Exception as e:
                    logger.warning("redis.async_ping_failed_fallback error=%s", str(e))
                    try:
                        await inst.aclose()
                    except Exception:
                        pass
            except ImportError:
                logger.warning("redis.async_not_installed_fallback")
            except Exception as e:
                logger.warning("redis.async_connect_failed error=%s", str(e))
        fake = _create_fakeredis()
        if fake is not None:
            _redis_instance = fake
            logger.info("redis.fakeredis_async_active")
            return _redis_instance
        return None


def set_redis_instance(inst: Any) -> None:
    """Test hook: inject a Redis instance (e.g. fakeredis)."""
    global _redis_instance
    with _redis_thread_lock:
        _redis_instance = inst


def clear_redis_instance() -> None:
    global _redis_instance
    with _redis_thread_lock:
        _redis_instance = None


# ── Cache decorator (ported from python-redis-module-skill) ──


def _cache_build_key(key_prefix: str, func: Callable, args: tuple, kwargs: dict) -> str:
    """Build stable key: prefix + signature-ordered params (self/cls dropped), kwargs bound by position."""
    fallback = f"{_REDIS_PREFIX_CACHE}{key_prefix}"
    try:
        import inspect as _inspect

        sig = _inspect.signature(func)
        bound = sig.bind_partial(*args, **kwargs)
        bound.apply_defaults()
        parts: list[str] = []
        for name in sig.parameters:
            if name in ("self", "cls"):
                continue
            if name in bound.arguments:
                v = bound.arguments[name]
                try:
                    if v is None or isinstance(v, (str, int, float, bool)):
                        parts.append(str(v))
                    else:
                        parts.append(json.dumps(v, sort_keys=True, ensure_ascii=False, default=str))
                except Exception:
                    parts.append(str(v))
        if parts:
            return f"{_REDIS_PREFIX_CACHE}{key_prefix}:{':'.join(parts)}"
        return fallback
    except Exception:
        return fallback


def _to_cacheable(obj: Any) -> Any:
    """Convert result to JSON-native structure; raise TypeError for unhandled types (skip caching)."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, list):
        return [_to_cacheable(x) for x in obj]
    if isinstance(obj, tuple):
        return {"__hero_tuple__": [_to_cacheable(x) for x in obj]}
    if isinstance(obj, dict):
        return {str(k): _to_cacheable(v) for k, v in obj.items()}
    if hasattr(obj, "__dataclass_fields__"):
        fields = obj.__dataclass_fields__  # type: ignore[attr-defined]
        data = {f: _to_cacheable(getattr(obj, f)) for f in fields}
        return {"__hero_dataclass__": {"module": obj.__class__.__module__, "qualname": obj.__class__.__qualname__, "data": data}}
    raise TypeError(f"uncacheable type {type(obj).__name__}")


def _from_cacheable(obj: Any) -> Any:
    """Reverse _to_cacheable; dataclass reconstructs via import, falls back to plain data dict."""
    if isinstance(obj, dict):
        if set(obj.keys()) == {"__hero_tuple__"}:
            return tuple(_from_cacheable(x) for x in obj["__hero_tuple__"])
        if set(obj.keys()) == {"__hero_dataclass__"}:
            meta = obj["__hero_dataclass__"]
            try:
                import importlib as _il

                mod = _il.import_module(meta["module"])
                cls = mod
                for part in str(meta["qualname"]).split("."):
                    cls = getattr(cls, part)
                data = {k: _from_cacheable(v) for k, v in meta["data"].items()}
                return cls(**data)
            except Exception as e:
                logger.debug("redis.cache_dataclass_rebuild_failed error=%s", str(e))
                try:
                    return {k: _from_cacheable(v) for k, v in meta.get("data", {}).items()}
                except Exception:
                    return None
        return {k: _from_cacheable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_from_cacheable(x) for x in obj]
    return obj


def _cache_get_decoded(redis_client: Any, cache_key: str) -> tuple[bool, Any]:
    """Try L2 get; returns (hit, value). Miss on any error or empty."""
    try:
        cached = redis_client.get(cache_key)
    except Exception as e:
        logger.debug("redis.cache_get_failed error=%s", str(e))
        return False, None
    if not cached:
        return False, None
    try:
        if isinstance(cached, (bytes, bytearray)):
            cached = bytes(cached).decode("utf-8", errors="ignore")
        return True, _from_cacheable(json.loads(cached))
    except Exception as e:
        logger.debug("redis.cache_decode_failed error=%s", str(e))
        return False, None


def _cache_set_encoded(redis_client: Any, cache_key: str, result: Any, expire: int) -> None:
    """Encode + set with ex; skips (fail-open) when result not cacheable."""
    try:
        payload = json.dumps(_to_cacheable(result), ensure_ascii=False, default=str)
    except TypeError:
        return
    except Exception as e:
        logger.debug("redis.cache_encode_failed error=%s", str(e))
        return
    try:
        redis_client.set(cache_key, payload, ex=expire)
    except Exception as e:
        logger.debug("redis.cache_set_failed error=%s", str(e))


def cache(key_prefix: str, expire: int = 300):
    """Sync/async isomorphic cache decorator — get/set via Redis, JSON serialized.

    Sync functions stay sync, async functions stay async (both use get_redis_sync).
    Key: hero:cache:{prefix}:{signature-ordered params} (self/cls dropped).
    Non-JSON-native results (e.g. DataFrame) skip caching fail-open.

    Example:
        @cache("market:tencent", expire=60)
        def get_bars(symbol, start, end): ...
    """

    def decorator(func: Callable) -> Callable:
        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs) -> Any:
                try:
                    cache_key = _cache_build_key(key_prefix, func, args, kwargs)
                except Exception:
                    return await func(*args, **kwargs)
                redis_client = get_redis_sync()
                if redis_client is not None:
                    hit, val = _cache_get_decoded(redis_client, cache_key)
                    if hit:
                        return val
                result = await func(*args, **kwargs)
                if redis_client is not None:
                    _cache_set_encoded(redis_client, cache_key, result, expire)
                return result

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs) -> Any:
            try:
                cache_key = _cache_build_key(key_prefix, func, args, kwargs)
            except Exception:
                return func(*args, **kwargs)
            redis_client = get_redis_sync()
            if redis_client is not None:
                hit, val = _cache_get_decoded(redis_client, cache_key)
                if hit:
                    return val
            result = func(*args, **kwargs)
            if redis_client is not None:
                _cache_set_encoded(redis_client, cache_key, result, expire)
            return result

        return sync_wrapper

    return decorator


# ── Distributed lock (ported) ──


class RedisLock:
    """Distributed lock via SET NX EX + DEL, async context manager."""

    def __init__(self, key_prefix: str = "hero:lock:"):
        self.key_prefix = key_prefix

    @asynccontextmanager
    async def lock(self, key: str, timeout: int = 30, retry: int = 3, delay: float = 0.2):
        redis_client = await get_redis()
        if redis_client is None:
            # No Redis — fail open with a dummy lock (single process)
            yield True
            return
        lock_key = f"{self.key_prefix}{key}"
        acquired = False
        for _ in range(retry):
            try:
                ok = await redis_client.set(lock_key, "1", nx=True, ex=timeout)
                if ok:
                    acquired = True
                    break
            except Exception as e:
                logger.debug("redis.lock_set_failed error=%s", str(e))
                break
            await asyncio.sleep(delay)
        if not acquired:
            raise TimeoutError(f"获取锁 {key} 失败")
        try:
            yield True
        finally:
            try:
                await redis_client.delete(lock_key)
            except Exception:
                pass


# ── Rate limiter (zset sliding window) ──


class RateLimiter:
    """Sliding window rate limiter via sorted set."""

    def __init__(self, key_prefix: str = _REDIS_PREFIX_RATELIMIT):
        self.key_prefix = key_prefix

    async def try_acquire(self, key: str, max_requests: int, window_seconds: int = 60) -> bool:
        redis_client = await get_redis()
        if redis_client is None:
            return True  # No Redis — allow
        window_key = f"{self.key_prefix}{key}"
        now = time.time()
        try:
            await redis_client.zremrangebyscore(window_key, 0, now - window_seconds)
            current = await redis_client.zcard(window_key)
            if current >= max_requests:
                return False
            await redis_client.zadd(window_key, {str(now): now})
            await redis_client.expire(window_key, window_seconds)
            return True
        except Exception as e:
            logger.debug("redis.ratelimit_failed error=%s", str(e))
            return True

    def try_acquire_sync(self, key: str, max_requests: int, window_seconds: int = 60) -> bool:
        """Sync variant for non-async call sites (e.g. FastAPI sync endpoint)."""
        redis_client = get_redis_sync()
        if redis_client is None:
            return True
        window_key = f"{self.key_prefix}{key}"
        now = time.time()
        try:
            redis_client.zremrangebyscore(window_key, 0, now - window_seconds)
            current = redis_client.zcard(window_key)
            if current >= max_requests:
                return False
            redis_client.zadd(window_key, {str(now): now})
            redis_client.expire(window_key, window_seconds)
            return True
        except Exception as e:
            logger.debug("redis.ratelimit_sync_failed error=%s", str(e))
            return True


# ── Counter (atomic INCR thin wrapper) ──


class Counter:
    """Distributed atomic counter via Redis INCR."""

    async def incr(self, key: str, amount: int = 1) -> int:
        client = await get_redis()
        if client is None:
            return 0
        try:
            return int(await client.incr(key, amount))
        except Exception as e:
            logger.debug("redis.counter_incr_failed error=%s", str(e))
            return 0

    def incr_sync(self, key: str, amount: int = 1) -> int:
        """Sync variant for non-async call sites."""
        client = get_redis_sync()
        if client is None:
            return 0
        try:
            return int(client.incr(key, amount))
        except Exception as e:
            logger.debug("redis.counter_incr_sync_failed error=%s", str(e))
            return 0


# ── Stream (xadd / xread) ──


class RedisStream:
    """Redis Stream publish/subscribe via xadd/xread."""

    STREAM_MAXLEN = 10000

    def __init__(self, client: Any | None = None):
        self._client = client

    async def _get_client(self):
        if self._client is not None:
            return self._client
        return await get_redis()

    async def publish(self, stream: str, data: Dict[str, Any]) -> str:
        client = await self._get_client()
        if client is None:
            return ""
        try:
            # Ensure all values are strings for Redis
            str_data = {k: str(v) for k, v in data.items()}
            try:
                return await client.xadd(stream, str_data, maxlen=self.STREAM_MAXLEN, approximate=True)
            except TypeError:
                # Older fakeredis/redis without maxlen kwargs — plain xadd then trim
                msg_id = await client.xadd(stream, str_data)
                try:
                    await client.xtrim(stream, maxlen=self.STREAM_MAXLEN, approximate=True)
                except Exception:
                    pass
                return msg_id
        except Exception as e:
            logger.debug("redis.stream_publish_failed error=%s", str(e))
            return ""

    async def publish_sync(self, stream: str, data: Dict[str, Any]) -> str:
        client = get_redis_sync()
        if client is None:
            return ""
        try:
            str_data = {k: str(v) for k, v in data.items()}
            return client.xadd(stream, str_data)
        except Exception as e:
            logger.debug("redis.stream_publish_sync_failed error=%s", str(e))
            return ""

    async def subscribe_consumer(
        self,
        stream: str,
        group: str,
        consumer: str,
        count: int = 10,
        block: int = 1000,
    ) -> list[tuple[str, Dict[str, Any]]]:
        """Read pending/new entries via consumer group; fall back to xread for fakeredis."""
        client = await self._get_client()
        if client is None:
            return []
        # Ensure the consumer group exists (idempotent; mkstream for first publish races)
        try:
            await client.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception as e:
            msg = str(e).lower()
            if "busygroup" not in msg and "exists" not in msg:
                logger.debug("redis.stream_group_create_failed error=%s", str(e))
        try:
            resp = await client.xreadgroup(group, consumer, {stream: ">"}, count=count, block=block)
        except Exception as e:
            logger.debug("redis.stream_xreadgroup_fallback error=%s", str(e))
            try:
                resp = await client.xread({stream: "0"}, count=count, block=block)
            except Exception as e2:
                logger.debug("redis.stream_xread_failed error=%s", str(e2))
                return []
        out: list[tuple[str, Dict[str, Any]]] = []
        try:
            for _stream, messages in resp or []:
                for msg_id, fields in messages:
                    out.append((msg_id if isinstance(msg_id, str) else str(msg_id), dict(fields)))
        except Exception as e:
            logger.debug("redis.stream_parse_failed error=%s", str(e))
            return []
        return out

    async def ack(self, stream: str, group: str, *ids: str) -> int:
        """Acknowledge consumed entries; returns acked count (0 when unsupported)."""
        if not ids:
            return 0
        client = await self._get_client()
        if client is None:
            return 0
        try:
            return int(await client.xack(stream, group, *ids))
        except Exception as e:
            logger.debug("redis.stream_ack_failed error=%s", str(e))
            return 0

    async def subscribe(
        self,
        stream: str,
        group: str,
        consumer: str,
        count: int = 10,
        block: int = 1000,
    ) -> list[tuple[str, Dict[str, Any]]]:
        """Alias of subscribe_consumer (consumer-group read)."""
        return await self.subscribe_consumer(stream, group, consumer, count=count, block=block)

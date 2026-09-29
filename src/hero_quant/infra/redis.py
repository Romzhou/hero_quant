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
import secrets
import threading as _threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)

# 中文：限流/缓存键前缀（ticket 键前缀收敛到 api.security._REDIS_TICKET_PREFIX，避免多处命名空间分叉）。
_REDIS_PREFIX_RATELIMIT = "hero:ratelimit:"
_REDIS_PREFIX_CACHE = "hero:cache:"

# 中文：sync/async 客户端分离存储，互不污染（单全局曾导致 async 路径拿到 sync 客户端）。
_redis_sync_instance: Any | None = None
_redis_async_instance: Any | None = None
_redis_thread_lock = _threading.Lock()
_redis_async_lock: asyncio.Lock | None = None  # legacy 兼容保留；get_redis 已改用 per-loop 锁
# 中文：per-loop 异步守卫（loop 对象 → asyncio.Lock；loop 关闭即清理，避免全局锁跨 loop 复用 RuntimeError）。
_redis_async_locks: dict[Any, asyncio.Lock] = {}
# 中文：sync 限流回退序列的进程内串行锁（只保护同步多步序列，不横跨 await；跨进程精确限流仍需 Lua）。
_RATELIMIT_SYNC_LOCK = _threading.Lock()

try:  # 中文：窄化捕获 eval/命令错误类型（fakeredis 不支持 eval 时走兼容路径）。
    from redis.exceptions import RedisError as _RedisError
    from redis.exceptions import ResponseError as _RedisResponseError

    _REDIS_ERRORS = (_RedisError, OSError, ValueError, TypeError, AttributeError, RuntimeError)
    _EVAL_ERRORS = (_RedisResponseError, TypeError, ValueError, AttributeError, RuntimeError)
except ImportError:  # 中文：未安装 redis-py 时退化为标准异常元组。
    _REDIS_ERRORS = (OSError, ValueError, TypeError, AttributeError, RuntimeError)
    _EVAL_ERRORS = (TypeError, ValueError, AttributeError, RuntimeError)


def _get_async_lock() -> asyncio.Lock:
    """返回当前 loop 的异步路径锁（per-loop；threading.Lock 只在创建瞬间保护，不横跨 await）。

    全局单例 asyncio.Lock 会绑定到首次创建的 loop，loop 关闭后复用抛 RuntimeError；
    此处按 loop 分发表，loop 切换即重建。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        global _redis_async_lock
        if _redis_async_lock is None:
            _redis_async_lock = asyncio.Lock()
        return _redis_async_lock
    with _redis_thread_lock:
        lock = _redis_async_locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            _redis_async_locks[loop] = lock
        return lock


def _get_client_kind(inst: Any) -> str:
    """判定客户端类型：'async' / 'sync' / 'unknown'（构造时类型探测，避免运行时误判）。"""
    try:
        import redis.asyncio as _aioredis  # type: ignore

        if isinstance(inst, _aioredis.Redis):
            return "async"
    except ImportError:
        pass
    try:
        import redis as _sync_redis  # type: ignore

        if isinstance(inst, _sync_redis.Redis):
            return "sync"
    except ImportError:
        pass
    try:
        import fakeredis.aioredis as _fake_async  # type: ignore

        if isinstance(inst, _fake_async.FakeRedis):
            return "async"
    except ImportError:
        pass
    try:
        import fakeredis as _fake_sync  # type: ignore

        if isinstance(inst, _fake_sync.FakeRedis):
            return "sync"
    except ImportError:
        pass
    get = getattr(inst, "get", None)
    if asyncio.iscoroutinefunction(get):
        return "async"
    if callable(get):
        return "sync"
    return "unknown"


async def _await_if_needed(value: Any) -> Any:
    """兼容 sync/async 客户端：协程则 await，否则直接返回。"""
    if asyncio.iscoroutine(value):
        return await value
    return value


def _get_redis_dsn() -> str | None:
    """从 Settings 读取 Redis DSN（唯一 env gate）。"""
    try:
        from hero_quant.config.settings import Settings

        return Settings().redis_dsn
    except (ImportError, AttributeError, ValueError, TypeError, OSError, RuntimeError) as e:
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
    except (ImportError, OSError, ValueError, TypeError, AttributeError, RuntimeError) as e:
        logger.warning("redis.fakeredis_create_failed error=%s", str(e))
        return None


def _require_real_redis_allowed() -> bool:
    """生产强制真实 Redis 门禁：仅 development/测试允许 fakeredis 回退。

    中文：HERO_ENV==development 或 PYTEST_CURRENT_TEST 在环境中时允许回退；
    其余（production/staging/未设）一律拒绝 fakeredis（fail-closed 返回 None，
    调用方限流/票据随即拒绝）。显式注入（set_redis_instance 测试钩子）不受此限。
    """
    try:
        import os as _os_gate

        hero_env = (_os_gate.environ.get("HERO_ENV", "") or "").strip().lower()
        if hero_env == "development":
            return True
        if "PYTEST_CURRENT_TEST" in _os_gate.environ:
            return True
        return False
    except (ValueError, TypeError, AttributeError):
        return False


def get_redis_sync():
    """Sync getter for non-async contexts (security.py). Returns sync redis or fakeredis sync."""
    global _redis_sync_instance
    if _redis_sync_instance is not None:
        return _redis_sync_instance
    with _redis_thread_lock:
        if _redis_sync_instance is not None:
            return _redis_sync_instance
        dsn = _get_redis_dsn()
        if dsn:
            try:
                import redis as _redis_sync  # type: ignore

                inst = _redis_sync.from_url(dsn, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
                try:
                    inst.ping()
                except _REDIS_ERRORS as e:
                    logger.warning("redis.ping_failed_fallback_fakeredis error=%s", str(e))
                    # 中文：ping 失败同样受生产门禁约束；development/测试才允许 fakeredis。
                    if not _require_real_redis_allowed():
                        logger.warning("redis.production_requires_real_redis_no_fakeredis_fallback")
                        return None
                    fake = _create_fakeredis_sync_fallback()
                    if fake is not None:
                        _redis_sync_instance = fake
                        return _redis_sync_instance
                    # 中文：ping 失败且无回退时不缓存坏客户端（fail-closed 返回 None）。
                    return None
                _redis_sync_instance = inst
                logger.info("redis.connected redacted=%s", _redact_dsn(dsn))
                return _redis_sync_instance
            except ImportError:
                logger.warning("redis.not_installed_try_fakeredis")
            except _REDIS_ERRORS as e:
                logger.warning("redis.connect_failed_try_fakeredis error=%s", str(e))
        # Fallback: fakeredis sync（仅 development/测试允许；生产 fail-closed 返回 None）
        if not _require_real_redis_allowed():
            logger.warning("redis.production_requires_real_redis_no_fakeredis_fallback")
            return None
        fake = _create_fakeredis_sync_fallback()
        if fake is not None:
            _redis_sync_instance = fake
            logger.info("redis.fakeredis_sync_active")
            return _redis_sync_instance
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

        # 中文：同时覆盖 user:pass@ 与 :pass@（空用户名）两种带口令形态。
        return re.sub(r"://([^/@]*):[^@]*@", r"://\1:***@", dsn)
    except (TypeError, ValueError, AttributeError):
        return "***"


async def get_redis():
    """Async Redis getter — returns redis.asyncio.Redis or fakeredis FakeRedis.

    优先 HERO_REDIS_DSN；DSN 缺失/不可达时仅 development/测试回退 fakeredis，
    生产 fail-closed 返回 None（调用方限流随即拒绝，防无保护超发）。
    中文：async 路径用 per-loop asyncio 锁（同步锁绝不横跨 await，避免阻塞事件循环线程；
    全局单例 asyncio.Lock 会绑定首个 loop，loop 关闭后复用抛 RuntimeError，故按 loop 分发）。
    """
    global _redis_async_instance
    if _redis_async_instance is not None:
        return _redis_async_instance
    lock = _get_async_lock()
    async with lock:
        if _redis_async_instance is not None:
            return _redis_async_instance
        dsn = _get_redis_dsn()
        if dsn:
            try:
                import redis.asyncio as aioredis  # type: ignore

                inst = aioredis.from_url(dsn, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
                try:
                    await inst.ping()
                    _redis_async_instance = inst
                    logger.info("redis.connected_async redacted=%s", _redact_dsn(dsn))
                    return _redis_async_instance
                except _REDIS_ERRORS as e:
                    logger.warning("redis.async_ping_failed_fallback error=%s", str(e))
                    try:
                        await inst.aclose()
                    except _REDIS_ERRORS:
                        pass
                    # 中文：ping 失败时不缓存坏客户端，继续走 fakeredis 回退。
            except ImportError:
                logger.warning("redis.async_not_installed_fallback")
            except _REDIS_ERRORS as e:
                logger.warning("redis.async_connect_failed error=%s", str(e))
        # 中文：fakeredis 回退仅 development/测试允许；生产 fail-closed 返回 None。
        if not _require_real_redis_allowed():
            logger.warning("redis.production_requires_real_redis_no_fakeredis_fallback")
            return None
        fake = _create_fakeredis()
        if fake is not None:
            _redis_async_instance = fake
            logger.info("redis.fakeredis_async_active")
            return _redis_async_instance
        return None


def set_redis_instance(inst: Any, *, for_sync: bool = False, for_async: bool = False) -> None:
    """Test hook: inject a Redis instance (e.g. fakeredis).

    双槽分离存储，避免 sync/async 类型污染：无 flag 的裸调用按客户端类型路由
    （sync→sync 槽，async→async 槽；类型未知的小替身保留旧语义双槽同注以兼容）；
    显式 flag 则只注入声明的槽，且类型冲突时拒绝（sync 槽拒收 async 客户端，反之亦然）。
    同步注入亦可用 set_redis_sync_instance。
    """
    global _redis_async_instance, _redis_sync_instance
    kind = _get_client_kind(inst)
    if not for_sync and not for_async:
        if kind == "sync":
            _redis_sync_instance = inst
            return
        if kind == "async":
            _redis_async_instance = inst
            return
        # 中文：类型未知（无 get/EVAL 的小替身）→ 保留旧双槽同注，调用方可走任一 getter。
        _redis_async_instance = inst
        _redis_sync_instance = inst
        return
    if for_async:
        if kind == "sync":
            logger.warning("redis.inject_async_slot_rejected_sync_client")
        else:
            _redis_async_instance = inst
    if for_sync:
        if kind == "async":
            logger.warning("redis.inject_sync_slot_rejected_async_client")
        else:
            _redis_sync_instance = inst


def set_redis_sync_instance(inst: Any) -> None:
    """Test hook: inject the sync-slot client only（拒绝 async 客户端，避免 sync 路径拿到协程）。"""
    global _redis_sync_instance
    kind = _get_client_kind(inst)
    if kind == "async":
        logger.warning("redis.inject_sync_slot_rejected_async_client")
        return
    _redis_sync_instance = inst


def clear_redis_instance() -> None:
    global _redis_async_instance, _redis_sync_instance, _redis_async_lock
    with _redis_thread_lock:
        _redis_async_instance = None
        _redis_sync_instance = None
        # 中文：同时清理 per-loop 锁表与 legacy 单例，避免关闭的 loop 绑定的锁被复用。
        _redis_async_locks.clear()
        _redis_async_lock = None


# ── Cache decorator (ported from python-redis-module-skill) ──


# 中文：缓存 dataclass 白名单（fail-closed：未注册类型只回退纯 dict，不做任意 import）。
_CACHE_DATACLASS_ALLOW: dict[str, Any] = {}


def register_cache_dataclass(cls: type) -> type:
    """注册允许从缓存重建的 dataclass 类型（白名单制，防任意 import 注入）。"""
    try:
        _CACHE_DATACLASS_ALLOW[f"{cls.__module__}.{cls.__qualname__}"] = cls
    except (AttributeError, TypeError):
        pass
    return cls


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
                except (ValueError, TypeError, AttributeError):
                    parts.append(str(v))
        if parts:
            return f"{_REDIS_PREFIX_CACHE}{key_prefix}:{':'.join(parts)}"
        return fallback
    except (ValueError, TypeError, AttributeError):
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
    """Reverse _to_cacheable；dataclass 仅重建白名单注册类型，否则回退纯 data dict（防任意 import 注入）。"""
    if isinstance(obj, dict):
        if set(obj.keys()) == {"__hero_tuple__"}:
            return tuple(_from_cacheable(x) for x in obj["__hero_tuple__"])
        if set(obj.keys()) == {"__hero_dataclass__"}:
            meta = obj["__hero_dataclass__"] if isinstance(obj.get("__hero_dataclass__"), dict) else {}
            try:
                data = {k: _from_cacheable(v) for k, v in meta.get("data", {}).items()} if isinstance(meta.get("data"), dict) else {}
            except (AttributeError, TypeError, ValueError):
                logger.debug("redis.cache_dataclass_data_bad")
                return {}
            # 中文：白名单查类（fail-closed：未注册只返回纯 dict，不做 import_module/getattr 链）。
            cls = None
            try:
                key = f"{meta.get('module')}.{meta.get('qualname')}"
                cls = _CACHE_DATACLASS_ALLOW.get(key)
            except (AttributeError, TypeError):
                cls = None
            if cls is None:
                return data
            try:
                return cls(**data)
            except (TypeError, ValueError, AttributeError) as e:
                logger.debug("redis.cache_dataclass_rebuild_failed error=%s", str(e))
                return data
        try:
            return {k: _from_cacheable(v) for k, v in obj.items()}
        except (AttributeError, TypeError, ValueError):
            logger.debug("redis.cache_dict_rebuild_failed")
            return {}
    if isinstance(obj, list):
        return [_from_cacheable(x) for x in obj]
    return obj


def _cache_get_decoded(redis_client: Any, cache_key: str) -> tuple[bool, Any]:
    """Try L2 get; returns (hit, value). Miss on any error or empty."""
    try:
        cached = redis_client.get(cache_key)
    except _REDIS_ERRORS as e:
        logger.debug("redis.cache_get_failed error=%s", str(e))
        return False, None
    if not cached:
        return False, None
    try:
        if isinstance(cached, (bytes, bytearray)):
            cached = bytes(cached).decode("utf-8")
        return True, _from_cacheable(json.loads(cached))
    except (ValueError, TypeError, AttributeError, UnicodeDecodeError) as e:
        logger.debug("redis.cache_decode_failed error=%s", str(e))
        return False, None


async def _cache_aget_decoded(redis_client: Any, cache_key: str) -> tuple[bool, Any]:
    """异步 L2 get（await）；返回 (hit, value)，失败/空 miss。"""
    try:
        cached = await _await_if_needed(redis_client.get(cache_key))
    except _REDIS_ERRORS as e:
        logger.debug("redis.cache_get_failed error=%s", str(e))
        return False, None
    if not cached:
        return False, None
    try:
        if isinstance(cached, (bytes, bytearray)):
            cached = bytes(cached).decode("utf-8")
        return True, _from_cacheable(json.loads(cached))
    except (ValueError, TypeError, AttributeError, UnicodeDecodeError) as e:
        logger.debug("redis.cache_decode_failed error=%s", str(e))
        return False, None


def _cache_set_encoded(redis_client: Any, cache_key: str, result: Any, expire: int) -> None:
    """Encode + set with ex; skips (fail-open) when result not cacheable."""
    try:
        payload = json.dumps(_to_cacheable(result), ensure_ascii=False, default=str)
    except TypeError:
        return
    except (ValueError, AttributeError) as e:
        logger.debug("redis.cache_encode_failed error=%s", str(e))
        return
    try:
        redis_client.set(cache_key, payload, ex=expire)
    except _REDIS_ERRORS as e:
        logger.debug("redis.cache_set_failed error=%s", str(e))


async def _cache_aset_encoded(redis_client: Any, cache_key: str, result: Any, expire: int) -> None:
    """异步 encode + set（await）；不可缓存类型跳过。"""
    try:
        payload = json.dumps(_to_cacheable(result), ensure_ascii=False, default=str)
    except TypeError:
        return
    except (ValueError, AttributeError) as e:
        logger.debug("redis.cache_encode_failed error=%s", str(e))
        return
    try:
        await _await_if_needed(redis_client.set(cache_key, payload, ex=expire))
    except _REDIS_ERRORS as e:
        logger.debug("redis.cache_set_failed error=%s", str(e))


def cache(key_prefix: str, expire: int = 300):
    """Sync/async isomorphic cache decorator — get/set via Redis, JSON serialized.

    Sync 函数走 get_redis_sync 同步 get/set；async 函数走 get_redis 异步 get/set（不阻塞 loop）。
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
                # 中文：async 路径 await 异步客户端（sync get/set 会阻塞 loop 且协程永不命中）。
                try:
                    cache_key = _cache_build_key(key_prefix, func, args, kwargs)
                except (ValueError, TypeError, AttributeError):
                    return await func(*args, **kwargs)
                redis_client = await get_redis()
                if redis_client is not None:
                    hit, val = await _cache_aget_decoded(redis_client, cache_key)
                    if hit:
                        return val
                result = await func(*args, **kwargs)
                if redis_client is not None:
                    await _cache_aset_encoded(redis_client, cache_key, result, expire)
                return result

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs) -> Any:
            try:
                cache_key = _cache_build_key(key_prefix, func, args, kwargs)
            except (ValueError, TypeError, AttributeError):
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


_RATELIMIT_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local current = redis.call('ZCARD', key)
if current >= limit then
  return 0
end
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, window)
return 1
"""

# 中文：锁释放 Lua（token 比对后删；过期后他人加锁时旧持有者不得误删）。
_LOCK_RELEASE_LUA = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


async def _eval_or_fallback(client: Any, script: str, nkeys: int, *args: Any) -> Any:
    """优先 eval 原子执行；fakeredis 不支持 eval 时按“单锁收拢”语义走兼容路径（调用方保证原子性）。"""
    try:
        res = client.eval(script, nkeys, *args)
        return await _await_if_needed(res)
    except _EVAL_ERRORS:
        return None


class RedisLock:
    """Distributed lock via SET NX EX + token compare-del, async context manager."""

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
        # 中文：唯一 token 标识持有者（常量值曾导致过期后误删他人锁）。
        token = f"{uuid.uuid4().hex}:{secrets.token_hex(8)}"
        acquired = False
        for _ in range(retry):
            try:
                res = redis_client.set(lock_key, token, nx=True, ex=timeout)
                ok = await _await_if_needed(res)
                if ok:
                    acquired = True
                    break
            except _REDIS_ERRORS as e:
                logger.debug("redis.lock_set_failed error=%s", str(e))
                break
            await asyncio.sleep(delay)
        if not acquired:
            raise TimeoutError(f"获取锁 {key} 失败")
        try:
            yield True
        finally:
            # 中文：token 比对后删（Lua 首选）；eval 不可用时做“读后删”但必须再比 token
            # （GET-then-DEL 是 check-then-act：锁过期瞬间旧持有者会误删新持有者锁，
            # 故仅在“比对且值仍为己方 token”时才删；续用旧实现的 get 脚本即此语义）。
            # fakeredis 不支持 eval（ResponseError）时 eval 路径抛错→回退到此分支；
            # 真 Redis 上 Lua 可用，互斥由 Lua 保证。
            try:
                done = await _eval_or_fallback(redis_client, _LOCK_RELEASE_LUA, 1, lock_key, token)
                if done is None:
                    cur = await _await_if_needed(redis_client.get(lock_key))
                    if cur == token:
                        await _await_if_needed(redis_client.delete(lock_key))
                    elif cur is not None:
                        logger.warning("redis.lock_release_token_mismatch_skip key=%s", lock_key)
            except _REDIS_ERRORS:
                pass


# ── Rate limiter (zset sliding window) ──


class RateLimiter:
    """Sliding window rate limiter via sorted set（单 round-trip Lua 原子判定，防并发超发）。"""

    def __init__(self, key_prefix: str = _REDIS_PREFIX_RATELIMIT):
        self.key_prefix = key_prefix

    def _lua_args(self, window_key: str, max_requests: int, window_seconds: int) -> tuple:
        """构造 Lua 参数：成员唯一（uuid），同毫秒调用各自计数。"""
        now = time.time()
        member = f"{now}:{uuid.uuid4().hex}"
        return (window_key, str(now), str(window_seconds), str(max_requests), member)

    async def try_acquire(self, key: str, max_requests: int, window_seconds: int = 60) -> bool:
        """滑动窗口获取配额 — fail-closed：后端缺失/异常一律拒绝（False），不放行。"""
        redis_client = await get_redis()
        if redis_client is None:
            # 中文：无 Redis（生产 fail-closed / 未配置）→ 拒绝，避免无保护超发。
            logger.warning("redis.ratelimit_no_backend_reject key=%s", key)
            return False
        window_key = f"{self.key_prefix}{key}"
        try:
            res = await _eval_or_fallback(redis_client, _RATELIMIT_LUA, 1, *self._lua_args(window_key, max_requests, window_seconds))
            if res is not None:
                return bool(int(res))
            # 中文：eval 不可用（fakeredis）时退化为串行“单锁收拢”本地判定：仍用唯一成员计数；
            # 三轮询无锁会并发超发，故用 RedisLock（其 Lua 路径可用时原子，无 Lua 时退化为
            # 单实例内串行；跨进程精确限流仍要求 Lua-capable Redis）。
            now = time.time()
            member = f"{now}:{uuid.uuid4().hex}"
            lock = RedisLock(key_prefix=f"{self.key_prefix}lock:")
            try:
                async with lock.lock(f"{key}", timeout=5, retry=10, delay=0.05):
                    await _await_if_needed(redis_client.zremrangebyscore(window_key, 0, now - window_seconds))
                    current = await _await_if_needed(redis_client.zcard(window_key))
                    if int(current) >= max_requests:
                        return False
                    await _await_if_needed(redis_client.zadd(window_key, {member: now}))
                    await _await_if_needed(redis_client.expire(window_key, window_seconds))
                    return True
            except TimeoutError:
                # 拿不到收拢锁：fail-closed 拒绝，避免无保护超发。
                logger.warning("redis.ratelimit_fallback_lock_timeout")
                return False
        except _REDIS_ERRORS as e:
            logger.warning("redis.ratelimit_failed_reject error=%s", str(e))
            return False

    def try_acquire_sync(self, key: str, max_requests: int, window_seconds: int = 60) -> bool:
        """Sync variant for non-async call sites (e.g. FastAPI sync endpoint) — fail-closed."""
        redis_client = get_redis_sync()
        if redis_client is None:
            # 中文：无 Redis（生产 fail-closed / 未配置）→ 拒绝，避免无保护超发。
            logger.warning("redis.ratelimit_no_backend_reject key=%s", key)
            return False
        window_key = f"{self.key_prefix}{key}"
        try:
            res = redis_client.eval(_RATELIMIT_LUA, 1, *self._lua_args(window_key, max_requests, window_seconds))
            if res is not None:
                return bool(int(res))
            # 中文：eval 不可用（fakeredis）但 eval 调用本身没抛错、仅返回 None 时：
            # 进程内串行三轮询（仍用唯一成员计数）；跨进程精确限流仍需 Lua 原子。
            with _RATELIMIT_SYNC_LOCK:
                now = time.time()
                member = f"{now}:{uuid.uuid4().hex}"
                redis_client.zremrangebyscore(window_key, 0, now - window_seconds)
                current = redis_client.zcard(window_key)
                if int(current) >= max_requests:
                    return False
                redis_client.zadd(window_key, {member: now})
                redis_client.expire(window_key, window_seconds)
                return True
        except _EVAL_ERRORS:
            # 中文：eval 不可用（旧 fakeredis）时退化为进程内串行的唯一成员计数本地判定。
            try:
                with _RATELIMIT_SYNC_LOCK:
                    now = time.time()
                    member = f"{now}:{uuid.uuid4().hex}"
                    redis_client.zremrangebyscore(window_key, 0, now - window_seconds)
                    current = redis_client.zcard(window_key)
                    if int(current) >= max_requests:
                        return False
                    redis_client.zadd(window_key, {member: now})
                    redis_client.expire(window_key, window_seconds)
                    return True
            except _REDIS_ERRORS as e:
                logger.warning("redis.ratelimit_sync_failed_reject error=%s", str(e))
                return False
        except _REDIS_ERRORS as e:
            logger.warning("redis.ratelimit_sync_failed_reject error=%s", str(e))
            return False


# ── Counter (atomic INCR thin wrapper) ──


class Counter:
    """Distributed atomic counter via Redis INCR."""

    async def incr(self, key: str, amount: int = 1) -> int:
        client = await get_redis()
        if client is None:
            return 0
        try:
            return int(await _await_if_needed(client.incr(key, amount)))
        except _REDIS_ERRORS as e:
            logger.debug("redis.counter_incr_failed error=%s", str(e))
            return 0

    def incr_sync(self, key: str, amount: int = 1) -> int:
        """Sync variant for non-async call sites."""
        client = get_redis_sync()
        if client is None:
            return 0
        try:
            return int(client.incr(key, amount))
        except _REDIS_ERRORS as e:
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
                res = client.xadd(stream, str_data, maxlen=self.STREAM_MAXLEN, approximate=True)
                return await _await_if_needed(res)
            except TypeError:
                # Older fakeredis/redis without maxlen kwargs — plain xadd then trim
                msg_id = await _await_if_needed(client.xadd(stream, str_data))
                try:
                    await _await_if_needed(client.xtrim(stream, maxlen=self.STREAM_MAXLEN, approximate=True))
                except _REDIS_ERRORS:
                    pass
                return msg_id
        except _REDIS_ERRORS as e:
            logger.debug("redis.stream_publish_failed error=%s", str(e))
            return ""

    def publish_sync(self, stream: str, data: Dict[str, Any]) -> str:
        """同步发布（普通 def：同步 xadd，不阻塞事件循环；与 async publish 同 cap 防无界增长）。"""
        client = get_redis_sync()
        if client is None:
            return ""
        try:
            str_data = {k: str(v) for k, v in data.items()}
            try:
                return client.xadd(stream, str_data, maxlen=self.STREAM_MAXLEN, approximate=True)
            except TypeError:
                # 旧 fakeredis/redis 无 maxlen 形参 → 裸 xadd 后 xtrim 兜底
                msg_id = client.xadd(stream, str_data)
                try:
                    client.xtrim(stream, maxlen=self.STREAM_MAXLEN, approximate=True)
                except _REDIS_ERRORS:
                    pass
                return msg_id
        except _REDIS_ERRORS as e:
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
            await _await_if_needed(client.xgroup_create(stream, group, id="0", mkstream=True))
        except _REDIS_ERRORS as e:
            msg = str(e).lower()
            if "busygroup" not in msg and "exists" not in msg:
                logger.debug("redis.stream_group_create_failed error=%s", str(e))
        try:
            resp = await _await_if_needed(client.xreadgroup(group, consumer, {stream: ">"}, count=count, block=block))
        except _REDIS_ERRORS as e:
            logger.debug("redis.stream_xreadgroup_fallback error=%s", str(e))
            try:
                resp = await _await_if_needed(client.xread({stream: "0"}, count=count, block=block))
            except _REDIS_ERRORS as e2:
                logger.debug("redis.stream_xread_failed error=%s", str(e2))
                return []
        out: list[tuple[str, Dict[str, Any]]] = []
        try:
            for _stream, messages in resp or []:
                for msg_id, fields in messages:
                    out.append((msg_id if isinstance(msg_id, str) else str(msg_id), dict(fields)))
        except (TypeError, ValueError, AttributeError) as e:
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
            return int(await _await_if_needed(client.xack(stream, group, *ids)))
        except _REDIS_ERRORS as e:
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

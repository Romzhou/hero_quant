"""db.pool — 共享 psycopg 连接池（无 ORM，psycopg_pool + 手写 SQL）。

约束（AGENTS §5/§6）：
- 无 ORM；连接池按 DSN 单例复用，避免每次请求建池。
- 无 DSN / 驱动缺失 / 探活失败一律 fail-closed 抛 DatabaseUnavailable，
  调用方转 503 并带明确 reason，禁止「内存假装 PG 成功」。
- 日志只记脱敏 host，绝不回显 DSN 口令。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

_POOL_PROBE_SQL = "SELECT 1"
_PROBE_TIMEOUT_SECONDS = 2.0

_pools: dict[str, Any] = {}
_pools_lock = threading.Lock()


class DatabaseUnavailable(RuntimeError):
    """数据库不可用（未配置 / 驱动缺失 / 探活失败）—— API 层应转 503。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.error = "database_unavailable"
        message = f"database_unavailable: {reason}"
        if detail:
            message += f" ({detail})"
        super().__init__(message)


def _redacted_host(dsn: str | None) -> str:
    """仅保留 host[:port] 用于日志（user/password/query 全部丢弃）。"""
    try:
        rest = str(dsn or "")
        if "://" in rest:
            rest = rest.split("://", 1)[1]
        if "@" in rest:
            rest = rest.split("@", 1)[1]
        return rest.split("/", 1)[0].split("?", 1)[0].strip() or "***"
    except (ValueError, TypeError, AttributeError):
        return "***"


def _dsn_key(dsn: str) -> str:
    return hashlib.sha256(dsn.encode("utf-8")).hexdigest()[:12]


def create_pool(
    dsn: str,
    *,
    min_size: int = 1,
    max_size: int = 5,
    probe_timeout: float = _PROBE_TIMEOUT_SECONDS,
) -> Any:
    """建池并做一次 `SELECT 1` 探活；任何失败都关池并抛 DatabaseUnavailable。"""
    if not isinstance(dsn, str) or not dsn.strip():
        raise DatabaseUnavailable("dsn_not_configured")
    try:
        from psycopg_pool import ConnectionPool  # type: ignore
    except ImportError as exc:
        raise DatabaseUnavailable("psycopg_pool_unavailable", type(exc).__name__) from exc

    try:
        pool = ConnectionPool(
            conninfo=dsn,
            min_size=max(0, int(min_size)),
            max_size=max(1, int(max_size)),
            timeout=float(probe_timeout),
            open=True,
        )
    except Exception as exc:  # noqa: BLE001 - 建池失败必须转成显式不可用
        raise DatabaseUnavailable("pool_create_failed", type(exc).__name__) from exc

    try:
        with pool.connection(timeout=probe_timeout) as conn:
            conn.execute(_POOL_PROBE_SQL)
    except Exception as exc:  # noqa: BLE001 - 探活失败必须 fail-closed
        try:
            pool.close()
        except Exception:  # noqa: BLE001 - 清理期异常不掩盖根因
            logger.warning("db.pool.close_failed host=%s error_code=db.pool.close_failed", _redacted_host(dsn), exc_info=True)
        raise DatabaseUnavailable("probe_failed", f"host={_redacted_host(dsn)} {type(exc).__name__}") from exc

    logger.info("db.pool.ready host=%s error_code=db.pool.ready", _redacted_host(dsn))
    return pool


def get_pool(dsn: str | None = None) -> Any:
    """按 DSN 返回共享池；DSN 缺省时读 Settings.checkpoint_dsn（唯一 env gate）。"""
    if dsn is None:
        try:
            from hero_quant.config.settings import get_settings

            dsn = get_settings().checkpoint_dsn
        except Exception as exc:  # noqa: BLE001 - 配置读取失败等同不可用
            raise DatabaseUnavailable("settings_unavailable", type(exc).__name__) from exc
    if not isinstance(dsn, str) or not dsn.strip():
        raise DatabaseUnavailable("checkpoint_dsn_not_configured")

    key = _dsn_key(dsn)
    with _pools_lock:
        existing = _pools.get(key)
        if existing is not None:
            return existing
        pool = create_pool(dsn)
        _pools[key] = pool
        return pool


def close_pools() -> None:
    """关闭全部共享池（测试/重启钩子）。"""
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        try:
            pool.close()
        except Exception:  # noqa: BLE001 - 关闭期异常只告警
            logger.warning("db.pool.close_failed error_code=db.pool.close_failed", exc_info=True)


__all__ = ["DatabaseUnavailable", "close_pools", "create_pool", "get_pool"]

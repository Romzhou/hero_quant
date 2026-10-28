"""用例级隔离：每用例自动失效 data_mode 缓存与 Settings 缓存，避免 HERO_DATA_MODE 切换脏读。

另含 PostgreSQL 可用性守卫 helper（`pg_available()` / `pg_guard_reason()`），供
`tests/test_0904_c1.py`、`tests/test_checkpoint_pg.py`、`tests/test_j8_loop_ckpt_gov.py`、
`tests/test_retrieval_eval.py`、`tests/test_vector_maturity4.py` 这 5 个 PG 集成族文件在
模块级 `pytestmark` 中复用（见各文件顶部）。背景：本机 `localhost:5432` 是 TCP SYN 黑洞
（SYN 无响应、不返回 RST），直接让 psycopg 建连会无限挂起，全量 `pytest -q` 因此永远跑不完；
故探测必须短超时 + 结果缓存，无真实 PG 时 fail-soft 成 skip 而非挂死。
"""

from __future__ import annotations

import functools
import logging
import os
import re
import socket

import pytest

logger = logging.getLogger(__name__)

# 探测用 DSN 的 env 读取顺序（与 src/hero_quant/config/settings.py:68-72、:156-188、:203-214 一致）
_PG_DSN_ENV_KEYS: tuple[str, ...] = (
    "HERO_CHECKPOINT_DSN",
    "HERO_PG_DSN",
    "HERO_VECTOR_DSN",
    "HERO_PGVECTOR_DSN",
    "HERO_MEMORY_PG_DSN",
    "HERO_BILLING_DSN",
)
# PG DSN 允许的 scheme 前缀（含 SQLAlchemy / asyncpg 变体）
_PG_DSN_PREFIXES: tuple[str, ...] = (
    "postgresql://",
    "postgres://",
    "postgresql+psycopg://",
    "postgresql+asyncpg://",
)
_DEFAULT_PG_PORT = 5432
# 中文：TCP 探测 0.3s（本机黑洞下 create_connection 会等满该值后抛 OSError）；
# psycopg 探测 2s（settings 侧连接超时口径，见 src/hero_quant/checkpoint/postgres.py:620 的 timeout=5 注释）。
_TCP_PROBE_TIMEOUT = 0.3
_CONNECT_PROBE_TIMEOUT = 2


def _redact_dsn(dsn: str) -> str:
    """脱敏 DSN 中的口令（只用于日志/ skip reason，避免把 secret 打进报告）。"""
    if not isinstance(dsn, str):
        return repr(dsn)
    return re.sub(r"://([^/@]*):[^@]*@", r"://\1:***@", dsn)


def _strip_scheme(dsn: str) -> str | None:
    """去掉 PG DSN 的 scheme 返回 `[userinfo@]host[:port][/db][?kw]`，非 PG DSN 返回 None。"""
    raw = (dsn or "").strip()
    low = raw.lower()
    for prefix in _PG_DSN_PREFIXES:
        if low.startswith(prefix):
            return raw[len(prefix):]
    return None


def resolve_pg_dsn() -> str:
    """解析探测用 DSN：首个非空 `HERO_*_DSN` → 回落本地默认（对齐 Settings 的 `HERO_PG_HOST` 口径）。

    空值 / 纯空白 env 一律视为"未配置"并继续回落，绝不抛异常（空 DSN 由 `pg_guard_reason()`
    按"不可用 → skip"处理）。
    """
    for key in _PG_DSN_ENV_KEYS:
        raw = os.environ.get(key)
        if raw is None:
            continue
        val = raw.strip()
        if val:
            return val
    host = (os.environ.get("HERO_PG_HOST", "") or "").strip() or "localhost"
    return f"postgresql://{host}:{_DEFAULT_PG_PORT}/hero_quant"


def _parse_host_port(dsn: str) -> tuple[str, int] | None:
    """从 DSN 提取 (host, port)；无法解析时返回 None（不抛异常）。"""
    rest = _strip_scheme(dsn)
    if not rest:
        return None
    try:
        hostport = rest.split("@")[-1].split("/")[0].split("?")[0]
        if hostport.startswith("["):  # IPv6 字面量：[::1]:5432
            host, _, tail = hostport[1:].partition("]")
            port_s = tail[1:] if tail.startswith(":") else ""
        else:
            host, _, port_s = hostport.partition(":")
        if not host:
            return None
        return host, int(port_s) if port_s.isdigit() else _DEFAULT_PG_PORT
    except (ValueError, TypeError):
        return None


@functools.lru_cache(maxsize=None)
def _pg_tcp_open(host: str, port: int) -> bool:
    """TCP 探测 host:port，超时 `_TCP_PROBE_TIMEOUT`；黑洞下返回 False 而非挂死。"""
    try:
        with socket.create_connection((host, port), timeout=_TCP_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def _conninfo(dsn: str) -> str:
    """把 `postgresql+psycopg://` 之类 SQLAlchemy scheme 还原成 psycopg 可用的 `postgresql://`。"""
    rest = _strip_scheme(dsn)
    return f"postgresql://{rest}" if rest else dsn


@functools.lru_cache(maxsize=None)
def pg_guard_reason() -> str | None:
    """返回 PG 不可用原因；返回 None 表示 PG 可用、路由 PG 的用例应照常执行。

    探测三段（全部短超时，总耗时上限约 2.3s），且 `lru_cache` 保证 5 个 PG 文件只探一次：
    1. DSN 解析（空/非法 → 直接判定不可用，不抛异常）；
    2. `socket.create_connection`（0.3s）—— 本机 localhost:5432 SYN 黑洞的快速失败点；
    3. `psycopg.connect(connect_timeout=2)` —— TCP 在听时再校验驱动是否可用、库/口令是否可连。
    仅靠 `pytest.importorskip("psycopg")` 不够：驱动装了但服务端不可达时，硬连仍会挂死。
    """
    dsn = resolve_pg_dsn()
    target = _parse_host_port(dsn)
    if target is None:
        return f"PG guard: DSN 无法解析为 host:port（{_redact_dsn(dsn)!r}），无真实 PostgreSQL 可用"
    host, port = target
    if not _pg_tcp_open(host, port):
        return (
            f"PG guard: PostgreSQL 不可达（{host}:{port} TCP 探测 {_TCP_PROBE_TIMEOUT}s 超时/拒绝）；"
            "无真实库时直连会挂死 pytest（本机 localhost:5432 即为 SYN 黑洞），故 skip PG 集成族用例"
        )
    try:
        import psycopg  # noqa: PLC0415 —— 探测期按需导入，缺驱动时降级为 skip
    except Exception as exc:  # pragma: no cover - 视环境而定
        return f"PG guard: psycopg 不可导入（{type(exc).__name__}），无法验证 {host}:{port} 上的真实 PG"
    try:
        with psycopg.connect(_conninfo(dsn), connect_timeout=_CONNECT_PROBE_TIMEOUT):
            return None
    except Exception as exc:  # pragma: no cover - 视环境而定
        return (
            f"PG guard: psycopg 连接 {_redact_dsn(dsn)} 失败（connect_timeout={_CONNECT_PROBE_TIMEOUT}s，"
            f"{type(exc).__name__}）"
        )


def pg_available() -> bool:
    """PG 是否可用（`pg_guard_reason()` 的布尔封装，供需要条件分支的用例复用）。"""
    return pg_guard_reason() is None


@pytest.fixture(autouse=True)
def _clear_data_mode_cache():
    """自动失效 registry 缓存与 Settings lru_cache，确保 synthetic/live 切换即时生效。"""
    # 预先清理，避免上游用例残留
    try:
        from hero_quant.data.registry import clear_settings_cache

        clear_settings_cache()
    except Exception:
        pass
    try:
        from hero_quant.config.settings import get_settings

        get_settings.cache_clear()  # type: ignore[attr-defined]
    except Exception:
        pass
    yield
    # 用例后清理，防止污染下游
    try:
        from hero_quant.data.registry import clear_settings_cache

        clear_settings_cache()
    except Exception as e:
        import logging

        logging.getLogger(__name__).warning("conftest clear_settings_cache failed: %s", e, exc_info=True)
    try:
        from hero_quant.config.settings import get_settings

        get_settings.cache_clear()  # type: ignore[attr-defined]
    except Exception as e:
        import logging

        logging.getLogger(__name__).warning("conftest get_settings cache_clear failed: %s", e, exc_info=True)

"""用例级隔离：每用例自动失效 data_mode 缓存与 Settings 缓存，避免 HERO_DATA_MODE 切换脏读。

另含 PostgreSQL 可用性守卫 helper（`pg_available()` / `pg_guard_reason()`），供
`tests/test_0904_c1.py`、`tests/test_checkpoint_pg.py`、`tests/test_j8_loop_ckpt_gov.py`、
`tests/test_retrieval_eval.py`、`tests/test_vector_maturity4.py` 这 5 个 PG 集成族文件在
模块级 `pytestmark` 中复用（见各文件顶部）。探测口径：按 HERO_CHECKPOINT_DSN / HERO_PG_DSN /
HERO_PG_HOST 解析 host:port（测试默认 localhost，不硬编码远端 IP），短超时探测；连不上或没有
heroquant/hero_quant 库即 skip。真实 PG 用例统一经 `pg_test_dsn()` 取 DSN（同源 host/port + 
connect_timeout=2），避免守卫放行、用例却硬连另一个 host 导致挂死整轮。

b9：`resolve_pg_dsn()` 首次解析后**冻结**（守卫批准的目标 = 用例连接的目标），显式配置了 PG DSN
键却解析不出 PG 连接串时抛 `PgDsnConfigError`，不再静默回落到本地默认 DSN。
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
# 中文：/ready 与 settings 的默认库名是 heroquant（docker-compose / scripts/init 口径）。
_DEFAULT_PG_DATABASE = "heroquant"
_ACCEPTED_PG_DATABASES = frozenset({"heroquant", "hero_quant"})

# ---------------------------------------------------------------------------
# 仓库根 .env 隔离（测试确定性）
# ---------------------------------------------------------------------------
# 背景：settings.py 在 import 时 load_dotenv(override=False)，仓库根 .env 若带
# HERO_BILLING_DSN 会把「本地内存降级用例」偷偷接到真 PG，导致同一条用例
# 「有 .env / 无 .env」结论不同。这里在 conftest 最早加载时快照真实进程 env，
# 仅对「不是调用方显式导出」的 DSN 键写入空字符串：dotenv 的 override=False
# 不会覆盖已存在（即使为空）的键，因此 .env 的 DSN 被确定性中和。
# 需要真 PG 的集成用例必须显式导出 HERO_CHECKPOINT_DSN / HERO_PG_DSN（opt-in）。
_DSN_ENV_SNAPSHOT: dict[str, str] = {k: os.environ[k] for k in _PG_DSN_ENV_KEYS if k in os.environ}
_DSN_ENV_NEUTRALIZED: dict[str, str | None] = {}
for _key in _PG_DSN_ENV_KEYS:
    if _key not in _DSN_ENV_SNAPSHOT:
        _DSN_ENV_NEUTRALIZED[_key] = os.environ.get(_key)
        os.environ[_key] = ""


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


class PgDsnConfigError(RuntimeError):
    """显式配置了 PG DSN 键、却解析不出 PG 连接串（绝不静默回落本地默认 DSN）。"""


# 中文（b9）：守卫探针批准过的 DSN 与其来源键，首次解析后冻结；`_PG_DSN_SOURCE is None`
# 表示没有任何显式 HERO_*_DSN（走本地默认，无口令）。
_PG_DSN_FROZEN: str | None = None
_PG_DSN_SOURCE: str | None = None
_DRIFT_WARNED = False


def _configured_pg_dsn() -> tuple[str, str]:
    """首个非空白 `HERO_*_DSN` → `(dsn, 键名)`；未配置返回 `("", "")`。

    非空但不是 PG scheme 时抛 `PgDsnConfigError`——b9 起不再「静默跳过坏值 + 回落本地默认」
    （那会把「配错了」伪装成「没配」，CI 表现为连一个注定失败的 DSN 然后整模块 ERROR）。
    """
    for key in _PG_DSN_ENV_KEYS:
        raw = os.environ.get(key)
        if raw is None:
            continue
        val = raw.strip()
        if not val:
            continue
        if _strip_scheme(val) is None:
            raise PgDsnConfigError(
                f"{key} 已配置但不是 PG DSN（{_redact_dsn(val)!r}）：拒绝静默回落到本地默认 DSN"
            )
        return val, key
    return "", ""


def _default_pg_dsn() -> str:
    """本地默认 DSN（对齐 Settings 的 `HERO_PG_HOST` 口径，无口令）。"""
    host = (os.environ.get("HERO_PG_HOST", "") or "").strip() or "localhost"
    return f"postgresql://{host}:{_DEFAULT_PG_PORT}/{_DEFAULT_PG_DATABASE}"


def _has_userinfo(dsn: str) -> bool:
    """DSN 是否带 userinfo（`user:pass@` / `user@`）——没有就说明凭据要靠 libpq 环境变量。"""
    rest = _strip_scheme(dsn)
    if rest is None:
        return False
    return "@" in rest.split("/", 1)[0]


def _missing_credentials_hint(dsn: str) -> str:
    """DSN 无 user/password 时的明确提示（不让 skip reason 含糊成「连不上」）。"""
    if _has_userinfo(dsn):
        return ""
    if any((os.environ.get(k) or "").strip() for k in ("PGUSER", "PGPASSWORD", "PGPASSFILE")):
        return "；DSN 未含 user/password，已依赖 libpq 环境凭据（PGUSER/PGPASSWORD/PGPASSFILE）"
    return (
        "；DSN 含 host 但无 user/password，且无 PGPASSWORD/PGPASSFILE —— 服务端要求口令时必然 "
        "fe_sendauth: no password supplied，请显式导出带凭据的 HERO_CHECKPOINT_DSN/HERO_PG_DSN"
    )


def _warn_if_pg_dsn_drifted() -> None:
    """守卫批准的 DSN 与当前 env 不一致时告警一次（只提示，不改变连接目标）。

    中文（b9）：历史上有测试（`tests/test_retest271_lane_f2.py`）用裸 `os.environ.pop()` 改
    env（未走 monkeypatch，不会自动还原）——b9 已把该处改为 `monkeypatch.delenv`。此告警作为
    **防回归**保留：fixture 若跟着 env 走就会连到「无 userinfo 的本地默认」，CI 有 PG 时整模块
    `fe_sendauth` ERROR；现在即使再出现同类脏改也只会告警、不会换连接目标。
    """
    global _DRIFT_WARNED
    if _DRIFT_WARNED or _PG_DSN_FROZEN is None:
        return
    _DRIFT_WARNED = True
    try:
        configured, key = _configured_pg_dsn()
    except PgDsnConfigError as exc:
        logger.warning("pg_dsn_drift: %s", exc)
        return
    if (configured or _default_pg_dsn()) != _PG_DSN_FROZEN:
        logger.warning(
            "pg_dsn_drift: env 现在解析出 %s，但 PG 守卫已批准的目标来自 %s；用例仍连守卫目标，"
            "请改用 monkeypatch 修改 DSN env（不要裸 os.environ.pop）",
            key or "本地默认",
            _PG_DSN_SOURCE or "本地默认",
        )


def resolve_pg_dsn() -> str:
    """解析探测/用例统一的 PG DSN：首个非空 `HERO_*_DSN` → 本地默认；**首次解析后冻结**。

    中文（b9 CI 6 条 setup ERROR 的根因）：守卫探针在 collection 期算一次（`lru_cache`），
    用例在 runtime 才取 DSN。若两者之间有人改了 `os.environ`（当时的元凶：
    `tests/test_retest271_lane_f2.py` 直接 `os.environ.pop("HERO_CHECKPOINT_DSN")`，没走
    monkeypatch；b9 已改为 `monkeypatch.delenv`），fixture 就会掉到「无 userinfo 的本地默认 DSN」上；
    CI 有 PG 在听 5432，于是变成 `psycopg.OperationalError: ... fe_sendauth: no password supplied`
    整模块 ERROR，而本地无 PG 时同样的代码只会 skip（所以本地/CI 结论分叉）。冻结后 env 漂移不再
    影响已批准的 PG 目标；DSN 真要被换掉，请改 env 后新起进程。

    显式配了 DSN 键却不是 PG scheme 时抛 `PgDsnConfigError`——不静默 fallback 到本地默认。
    """
    global _PG_DSN_FROZEN, _PG_DSN_SOURCE
    if _PG_DSN_FROZEN is not None:
        return _PG_DSN_FROZEN
    configured, key = _configured_pg_dsn()
    if configured:
        _PG_DSN_FROZEN, _PG_DSN_SOURCE = configured, key
    else:
        _PG_DSN_FROZEN, _PG_DSN_SOURCE = _default_pg_dsn(), None
    return _PG_DSN_FROZEN


def pg_test_dsn(tag: str) -> str:
    """给 PG 集成用例生成可用 DSN：沿用探测 DSN 的 host/凭据/库，用 application_name 作命名空间。

    中文：老用例把 postgresql://postgres:postgres@localhost:5432/<唯一库名> 写死，既无法
    连到 env 指定的 llm-pg（凭据/主机不同），也会在库不存在时反复重连挂死。统一改走本 helper：
    连接目标来自 HERO_CHECKPOINT_DSN / HERO_PG_DSN / HERO_PG_HOST，tag 只用于让
    跨 DSN 隔离类用例仍然拿到字符串不同的 DSN（同一库上以 application_name 区分）。

    b9：连接目标取 `resolve_pg_dsn()` 的**冻结值**（= 守卫探针验证过的那一份），
    因此 collection 之后被其它用例改写的 env 不会把 fixture 甩到另一个 DSN 上。
    """
    _warn_if_pg_dsn_drifted()
    base = resolve_pg_dsn().strip()
    sep = "&" if "?" in base else "?"
    # connect_timeout=2：连接目标与 guard 同源，且任何真实 PG 连接都不允许走 OS 默认超时（防黑洞挂死）。
    return f"{base}{sep}application_name={tag}&connect_timeout=2"


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
    1. DSN 解析（未配置 → 本地默认；显式配置但非 PG scheme → 明确 skip，不再静默 fallback）；
    2. `socket.create_connection`（0.3s）—— 对 env 解析出的 host:port 做快速 TCP 探测；
    3. `psycopg.connect(connect_timeout=2)` —— TCP 在听时再校验驱动是否可用、库/口令是否可连。
    仅靠 `pytest.importorskip("psycopg")` 不够：驱动装了但服务端不可达时，硬连仍会挂死。

    skip reason 必须自证「为什么不可用」：来源键（或「无显式 DSN，走本地默认」）、以及
    「DSN 有 host 但没 user/password」这类注定 fe_sendauth 的情形会显式点明。
    """
    try:
        dsn = resolve_pg_dsn()
    except PgDsnConfigError as exc:
        return f"PG guard: {exc}；PG 集成用例跳过（不连注定失败的回落 DSN）"
    target = _parse_host_port(dsn)
    if target is None:
        return f"PG guard: DSN 无法解析为 host:port（{_redact_dsn(dsn)!r}），无真实 PostgreSQL 可用"
    host, port = target
    if not _pg_tcp_open(host, port):
        source = (
            f"DSN 来源 {_PG_DSN_SOURCE}" if _PG_DSN_SOURCE else "无显式 HERO_CHECKPOINT_DSN/HERO_PG_DSN，使用本地默认 DSN"
        )
        return (
            f"PG guard: PostgreSQL 不可达（{host}:{port} TCP 探测 {_TCP_PROBE_TIMEOUT}s 超时/拒绝；"
            f"{source}），故 skip PG 集成族用例"
        )
    try:
        import psycopg  # noqa: PLC0415 —— 探测期按需导入，缺驱动时降级为 skip
    except Exception as exc:  # pragma: no cover - 视环境而定
        return f"PG guard: psycopg 不可导入（{type(exc).__name__}），无法验证 {host}:{port} 上的真实 PG"
    try:
        with psycopg.connect(_conninfo(dsn), connect_timeout=_CONNECT_PROBE_TIMEOUT) as _conn:
            _db = (_conn.execute("SELECT current_database()").fetchone() or ("",))[0]
            _has_hq = _conn.execute(
                "SELECT 1 FROM pg_database WHERE datname IN ('heroquant', 'hero_quant') LIMIT 1"
            ).fetchone()
    except Exception as exc:  # pragma: no cover - 视环境而定
        return (
            f"PG guard: psycopg 连接 {_redact_dsn(dsn)} 失败（connect_timeout={_CONNECT_PROBE_TIMEOUT}s，"
            f"{type(exc).__name__}）" + _missing_credentials_hint(dsn)
        )
    # 中文：能连上但库里没有 heroquant/hero_quant（例如连到 postgres 维护库）不算可用，
    # 否则 PG 用例会在错误库上建表，或直接因 schema 不存在而失败。
    if _db not in _ACCEPTED_PG_DATABASES and not _has_hq:
        return (
            f"PG guard: 已连接 {_redact_dsn(dsn)}，但 current_database={_db!r} 且不存在 "
            "heroquant/hero_quant 库；PG 集成用例跳过"
        )
    return None


def pg_available() -> bool:
    """PG 是否可用（`pg_guard_reason()` 的布尔封装，供需要条件分支的用例复用）。"""
    return pg_guard_reason() is None


@pytest.fixture
def billing_memory(monkeypatch):
    """显式开启 billing 内存降级（新契约：无 PG 必须 opt-in，返回体带 degraded:true）。

    中文：S4 起 HERO_BILLING_ALLOW_MEMORY 是唯一降级开关，默认 fail-closed；
    内存幂等/并发/覆盖冲突类用例必须显式 opt-in，并断言 degraded+reason=billing:memory，
    不得在全局 conftest 默认打开（否则真 PG 语义被掩盖）。
    """
    monkeypatch.setenv("HERO_BILLING_ALLOW_MEMORY", "1")
    for key in _PG_DSN_ENV_KEYS:
        monkeypatch.setenv(key, "")
    from hero_quant.config.settings import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    yield


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


# ---------------------------------------------------------------------------
# PG skip 汇总 + 会话级 env 还原
# ---------------------------------------------------------------------------
_PG_SKIPPED_NODEIDS: list[str] = []


def pytest_runtest_logreport(report) -> None:
    """收集被 PG 守卫跳过的用例，session 结束时显式汇总（skip 不再藏在绿灯里）。"""
    if not report.skipped:
        return
    longrepr = report.longrepr
    reason = longrepr[2] if isinstance(longrepr, tuple) and len(longrepr) > 2 else str(longrepr)
    if "PG guard" in reason:
        _PG_SKIPPED_NODEIDS.append(report.nodeid)


def pytest_sessionfinish(session, exitstatus) -> None:
    if _PG_SKIPPED_NODEIDS:
        # 错误的入口必须显式提示（批次16 A 补充）：本地跑全量只有一个正确命令。
        bar = "=" * 78
        print(
            f"\n{bar}\n"
            f"[PG GUARD] {len(_PG_SKIPPED_NODEIDS)} 个 PG 集成用例因无可用 PostgreSQL 被整族 skip。\n"
            "  本地跑全量请用唯一入口：scripts/test_local.sh（自动注入 DSN）\n"
            "  Windows：scripts/test_local.ps1\n"
            "  直接用 pytest 会丢掉这族用例，本地绿不代表 CI 绿。\n"
            f"{bar}\n"
        )
    # 还原 conftest 加载时被中和的 dotenv DSN 键（避免污染同一进程内的后续调用）
    for key, value in _DSN_ENV_NEUTRALIZED.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

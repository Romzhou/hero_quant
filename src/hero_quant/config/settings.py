"""配置聚合入口 —— 唯一环境变量网关（env gate）。

职责：集中解析 HERO_* 环境变量并暴露为 Settings 数据类；架构位置：config 层最底层，其余模块仅依赖 Settings。
设计约定：所有 HERO_* 映射在此文件通过 os.getenv 完成，避免分散读取；支持 HERO_WALL_TIME_BUDGET_SECONDS 等别名的兼容解析。
"""

import copy
import logging
import os
import re
import warnings
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote as _urlquote
from urllib.parse import unquote as _urlunquote
from urllib.parse import urlsplit as _urlsplit
from urllib.parse import urlunsplit as _urlunsplit

logger = logging.getLogger(__name__)

# Auto-load .env so `uvicorn hero_quant.api.server:app` picks up HERO_* without manual export.
# Safe: override=False — explicit env vars still win; missing file is silently ignored.
try:
    from dotenv import load_dotenv as _load_dotenv

    # Walk up from this file to repo root to find .env (src/hero_quant/config/ -> repo root)
    _repo_root = Path(__file__).resolve().parents[3]
    for _candidate in (_repo_root / ".env", Path.cwd() / ".env"):
        if _candidate.is_file():
            _load_dotenv(dotenv_path=_candidate, override=False)
            break
    else:
        _load_dotenv(override=False)
except (ImportError, OSError, ValueError, TypeError, RuntimeError) as e:
    # 中文：窄化捕获 dotenv 加载异常并记录，避免静默吞没配置失败
    logger.debug("dotenv autoload skipped: %s", e)


def _redact_dsn(dsn: str) -> str:
    """Redact password in DSN before logging (replace password with ***)."""
    if not isinstance(dsn, str) or "://" not in dsn:
        return "***"
    try:
        # 中文：同时覆盖 user:pass@ 与 :pass@（空用户名）两种带口令形态
        return re.sub(r"://([^/@]*):[^@]*@", r"://\1:***@", dsn)
    except (TypeError, ValueError, AttributeError):
        return "***"


def _wall_time_budget_from_env() -> float | None:
    """解析 wall-time 预算（单位：秒），兼容两个环境变量键，仅接受正数。"""
    for key in ("HERO_WALL_TIME_BUDGET_SECONDS", "HERO_WALL_TIME_BUDGET"):
        raw = os.getenv(key, "")
        if raw and str(raw).strip():
            try:
                v = float(str(raw).strip())
                if v > 0:
                    return v
                warnings.warn(f"{key} must be >0, got {raw!r}", UserWarning, stacklevel=2)
                logger.warning("Invalid %s=%r must be >0", key, raw)
            except (ValueError, TypeError) as e:
                warnings.warn(f"Invalid {key}={raw!r}: {e}", UserWarning, stacklevel=2)
                logger.warning("Invalid %s=%r: %s", key, raw, e)
                continue
    return None


# 向量存储常量：pg DSN 前缀与多键兼容，保持单一 env gate 便于统一管控
_PG_PREFIXES = ("postgresql://", "postgres://", "postgresql+psycopg://")
_PGVECTOR_DSN_ENV_KEYS = (
    "HERO_VECTOR_DSN",
    "HERO_PGVECTOR_DSN",
    "HERO_MEMORY_PG_DSN",
    "HERO_PG_DSN",
    "HERO_VECTOR_URL",
    "HERO_PGVECTOR_URL",
)
_PGVECTOR_DIM_KEYS = ("HERO_VECTOR_DIM", "HERO_EMBED_DIM", "HERO_PGVECTOR_DIM", "HERO_VECTOR_SIZE")
_PROVIDER_ALIASES = {
    "openai": "openai",
    "sentence-transformers": "sentence-transformers",
    "sentence_transformers": "sentence-transformers",
    "sbert": "sentence-transformers",
    "all-minilm-l6-v2": "sentence-transformers",
    "offline": "offline",
    "hash": "offline",
    "fallback": "offline",
}


def _vector_dim_from_env() -> int:
    """解析向量维度，范围约束 8–2048，默认 32（兼顾内存与精度）。"""
    for key in _PGVECTOR_DIM_KEYS:
        raw = os.getenv(key, "")
        if raw and str(raw).strip():
            try:
                v = int(str(raw).strip())
                if 8 <= v <= 2048:
                    return v
                warnings.warn(f"{key}={raw!r} out of range 8-2048", UserWarning, stacklevel=2)
                logger.warning("Invalid %s=%r out of range 8-2048", key, raw)
            except (ValueError, TypeError) as e:
                warnings.warn(f"Invalid {key}={raw!r}: {e}", UserWarning, stacklevel=2)
                logger.warning("Invalid %s=%r: %s", key, raw, e)
                continue
    return 32


def _embed_provider_from_env() -> str:
    """解析嵌入提供方，归一化别名到 openai / sentence-transformers / offline。"""
    raw = os.getenv("HERO_EMBED_PROVIDER", "offline")
    if raw is None:
        raw = "offline"
    key = str(raw).strip().lower()
    if not key:
        return "offline"
    if key in _PROVIDER_ALIASES:
        return _PROVIDER_ALIASES[key]
    if "openai" in key:
        return "openai"
    if "sentence" in key or "sbert" in key:
        return "sentence-transformers"
    return "offline"


def _vector_dsn_from_env() -> str | None:
    """解析向量存储 DSN，按优先级尝试多键与回退到 checkpoint DSN。"""
    for k in _PGVECTOR_DSN_ENV_KEYS:
        raw = os.getenv(k, "") or ""
        if isinstance(raw, str) and raw.strip():
            s = raw.strip()
            if s.lower().startswith(_PG_PREFIXES):
                return s
            warnings.warn(f"{k} does not look like a PG DSN: {_redact_dsn(s)!r}", UserWarning, stacklevel=2)
            logger.warning("%s does not look like PG DSN: %r", k, _redact_dsn(s))
    store = (os.getenv("HERO_VECTOR_STORE", "") or "").strip().lower()
    if store in ("pgvector", "postgres", "pg", "auto"):
        c = (os.getenv("HERO_CHECKPOINT_DSN", "") or "").strip()
        if c.lower().startswith(_PG_PREFIXES):
            return c
    if (os.getenv("HERO_VECTOR_ENABLED", "") or "").strip().lower() in ("1", "true", "yes", "pgvector", "on"):
        c = (os.getenv("HERO_CHECKPOINT_DSN", "") or "").strip()
        if c.lower().startswith(_PG_PREFIXES):
            return c
    return None


def _pg_host_from_env() -> str:
    """PG 主机名唯一切换点（env gate）：本地默认 localhost，compose 内经 HERO_PG_HOST=postgres 切换。

    调用方（server.py 等）不得另写 localhost 硬编码默认值；容器连通性由
    docker-compose.yml 的 HERO_PG_HOST / HERO_CHECKPOINT_DSN 显式 env 保证。
    """
    raw = (os.getenv("HERO_PG_HOST", "") or "").strip()
    return raw or "localhost"


def _checkpoint_dsn_from_env() -> str:
    """Checkpoint PG DSN：显式配置优先；未配置时按 HERO_PG_HOST 回退无口令 PG 默认。

    说明：源码不再硬编码任何口令（曾用的 postgres:postgres 已移除）。
    Task7 PG-default 架构被既有单测锁定（test_checkpoint_pg /
    test_docs_honesty 要求默认 PG 前缀，且不得改跨文件签名），故保留 PG 形态
    默认；无真实 PG 时运行时走 emulated/memory 回退，不抛错。
    fail-closed None 默认需先更新上述 pinning 单测，已列为 open item 升级。
    """
    raw = os.getenv("HERO_CHECKPOINT_DSN", "")
    if raw and raw.strip():
        s = raw.strip()
        if s.lower().startswith(_PG_PREFIXES):
            return _apply_pg_password(s)
        warnings.warn(f"HERO_CHECKPOINT_DSN does not look like PG DSN: {_redact_dsn(s)!r}", UserWarning, stacklevel=2)
        logger.warning("HERO_CHECKPOINT_DSN invalid PG DSN: %r", _redact_dsn(s))
        # fall through to alias / fail-closed rather than returning garbage
    # also respect legacy HERO_PG_DSN alias (requires prefix consistently)
    alt = os.getenv("HERO_PG_DSN", "")
    if alt and alt.strip() and alt.strip().lower().startswith(_PG_PREFIXES):
        return _apply_pg_password(alt.strip())
    elif alt and alt.strip():
        warnings.warn(f"HERO_PG_DSN does not look like PG DSN: {_redact_dsn(alt)!r}", UserWarning, stacklevel=2)
        logger.warning("HERO_PG_DSN invalid PG DSN: %r", _redact_dsn(alt))
    # 未配置：按 HERO_PG_HOST 回退无口令 PG 默认（本地 localhost / compose 内 postgres；
    # Task7 pinning 单测锁定 PG 形态且默认不得含 userinfo，见 test_retest271_lane_e2；
    # 运行时无池走 emulated/memory）。注意：此默认不含任何口令；fail-closed None 需先更新 pinning 单测（open item）。
    # 库名统一为 heroquant：与 docker-compose.yml 的 HERO_CHECKPOINT_DSN、
    # POSTGRES_DB 及 scripts/init/001_schema.sql 的建库目标一致（曾用 hero_quant
    # 会连到另一个库，导致建表脚本与运行时各说各话）。
    return _apply_pg_password(f"postgresql://{_pg_host_from_env()}:5432/heroquant")


def _pg_password_from_provider() -> str | None:
    """PG 口令经 secrets provider 取（env 后端即 HERO_PG_PASSWORD）。

    PR-151 接线：`vault`/`kms` 未实现时返回 None —— **不回落** os.environ，
    DSN 保持无口令，连接失败按既有语义暴露（/ready 如实 503），不偷偷用明文。
    延迟导入 secrets：settings 是 env 网关，secrets 包又依赖 settings，避免循环导入。
    """
    try:
        from hero_quant.secrets.resolve import resolve_secret
    except ImportError as e:  # pragma: no cover - 只在残缺安装下触发
        logger.warning("settings.pg_password_unavailable error_code=secrets_unavailable detail=%s", e)
        return None
    value, reason = resolve_secret("pg_password")
    if reason:
        logger.warning("settings.pg_password_unavailable error_code=%s", reason)
    return value


def _apply_pg_password(dsn: str | None) -> str | None:
    """给「有用户名但没口令」的 PG DSN 注入 provider 提供的口令（PR-151）。

    规则（保守，全部显式）：
    - 非 PG scheme 不动；
    - DSN 已带口令 → 显式优先，不动；
    - 无 userinfo（连用户名都没有）→ 不动（只有口令没有用户拼不出合法 DSN）；
    - provider 取不到口令（未配置 / vault 不可用）→ 不动；
    - 口令按 RFC 3986 百分号编码后写入，特殊字符不会破坏 DSN。
    日志只记 host，永不记口令或完整 DSN。
    """
    if not dsn or not isinstance(dsn, str):
        return dsn
    try:
        parts = _urlsplit(dsn)
    except ValueError:
        return dsn
    if parts.scheme.lower() not in ("postgres", "postgresql"):
        return dsn
    if "@" not in parts.netloc:
        return dsn
    if parts.password:
        return dsn
    password = _pg_password_from_provider()
    if not password:
        return dsn
    try:
        port = parts.port
    except ValueError:
        logger.warning("settings.pg_password_injected_skipped error_code=invalid_port")
        return dsn
    user = _urlunquote(parts.username or "")
    host = parts.hostname or ""
    host_part = f"[{host}]" if ":" in host else host
    netloc = f"{_urlquote(user, safe='')}:{_urlquote(password, safe='')}@{host_part}"
    if port:
        netloc += f":{port}"
    logger.info("settings.pg_password_injected host=%s", host)
    return _urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _checkpoint_ttl_from_env() -> int:
    """checkpoint TTL seconds, default 7d, via Settings for expires_at."""
    raw = os.getenv("HERO_CHECKPOINT_TTL_SECONDS", "") or os.getenv("HERO_CHECKPOINT_TTL", "")
    if raw and str(raw).strip():
        try:
            v = int(str(raw).strip())
            if v > 0:
                return v
            warnings.warn(f"HERO_CHECKPOINT_TTL invalid <=0: {raw!r}", UserWarning, stacklevel=2)
            logger.warning("Invalid checkpoint TTL %r must be >0", raw)
        except (ValueError, TypeError) as e:
            warnings.warn(f"Invalid HERO_CHECKPOINT_TTL={raw!r}: {e}", UserWarning, stacklevel=2)
            logger.warning("Invalid checkpoint TTL %r: %s", raw, e)
            pass
    return 7 * 24 * 3600


def _billing_dsn_from_env() -> str | None:
    """Billing PG DSN：仅显式 HERO_BILLING_DSN 生效；默认 fail-closed 返回 None。

    与 checkpoint 共享 DB 必须经 HERO_BILLING_ALLOW_SHARED_DB=1/true/yes 显式 opt-in，
    否则财务数据与 checkpoint 混库有表冲突/资金完整性风险。"""
    # 中文：显式 billing DSN 若存在但非法前缀需告警而非静默忽略（fail-visible）
    raw = (os.getenv("HERO_BILLING_DSN", "") or "").strip()
    if raw:
        if raw.lower().startswith(_PG_PREFIXES):
            return _apply_pg_password(raw)
        warnings.warn(f"HERO_BILLING_DSN does not look like PG DSN: {_redact_dsn(raw)!r}", UserWarning, stacklevel=2)
        logger.warning("HERO_BILLING_DSN invalid PG DSN: %r", _redact_dsn(raw))
    # 共享 DB 必须显式 opt-in，否则 fail-closed（默认 None → 内存/独立回退）
    opt_in = (os.getenv("HERO_BILLING_ALLOW_SHARED_DB", "") or "").strip().lower() in ("1", "true", "yes", "on")
    if not opt_in:
        return None
    # Explicit opt-in fallback: warn about isolation when reusing checkpoint DSN
    for k in ("HERO_PG_DSN", "HERO_CHECKPOINT_DSN"):
        raw = os.getenv(k, "") or ""
        if isinstance(raw, str) and raw.strip() and raw.strip().lower().startswith(_PG_PREFIXES):
            warnings.warn(
                f"HERO_BILLING_DSN not set, falling back to {k} – billing and checkpoint will share DB",
                UserWarning,
                stacklevel=2,
            )
            logger.warning("HERO_BILLING_DSN not set, falling back to %s – shared DB", k)
            return _apply_pg_password(raw.strip())
    return None


def _redis_password_from_provider(legacy: str | None) -> tuple[str, str | None]:
    """Redis 口令经 secrets provider 取（env 后端下才回退 legacy=HERO_REDIS_PASSWORD）。

    PR-151 收尾（批次14）：vault/kms 未实现时返回 ("", reason)，**不回落** env——
    DSN 保持无口令，连接失败按既有语义暴露（/ready 如实 503），不偷偷用明文。
    延迟导入 secrets：settings 是 env 网关，secrets 包又依赖 settings，避免循环导入。
    """
    try:
        from hero_quant.secrets.resolve import resolve_secret
    except ImportError as e:  # pragma: no cover - 只在残缺安装下触发
        logger.warning("settings.redis_password_unavailable error_code=secrets_unavailable detail=%s", e)
        return (legacy or ""), None
    value, reason = resolve_secret("redis_password", legacy)
    if reason:
        logger.warning("settings.redis_password_unavailable error_code=%s", reason)
    return (value or ""), reason


def _apply_redis_password(dsn: str | None) -> str | None:
    """给「有 userinfo 但没口令」的 Redis DSN 注入 provider 口令（PR-151 收尾）。

    规则与 secrets.redis.apply_redis_password 完全一致，这里只是延迟导入的薄壳：
    settings 模块导入时不拉 secrets 包，避免 settings<->secrets 循环。
    """
    if not dsn:
        return dsn
    try:
        from hero_quant.secrets.redis import apply_redis_password
    except ImportError as e:  # pragma: no cover - 只在残缺安装下触发
        logger.warning("settings.redis_password_inject_skipped error_code=secrets_unavailable detail=%s", e)
        return dsn
    return apply_redis_password(dsn)


def _redis_dsn_from_env() -> str | None:
    """Redis DSN 解析，遵循单一 env gate 约束。

    优先级：HERO_REDIS_DSN（含 redis:// 前缀校验） > HERO_REDIS_HOST/PORT/PASSWORD/DB 拼装 > REDIS_URL 兼容。
    返回 None 表示未配置（调用方可回退 fakeredis 或 fail-closed）。
    """
    raw = os.getenv("HERO_REDIS_DSN", "")
    if raw and raw.strip():
        s = raw.strip()
        if s.lower().startswith(("redis://", "rediss://", "unix://")):
            # PR-151 收尾：无口令时按 provider 注入（vault/kms 不可用则原样返回）
            return _apply_redis_password(s)
        warnings.warn(f"HERO_REDIS_DSN does not look like redis DSN: {_redact_dsn(s)!r}", UserWarning, stacklevel=2)
        logger.warning("HERO_REDIS_DSN invalid redis DSN: %r", _redact_dsn(s))
        # fall through：非法 DSN 不直接返回，继续走 HERO_REDIS_HOST 拼装/REDIS_URL 兼容，
        # 避免一次笔误在 HOST/URL 有效时彻底禁用 Redis（fail-visible：已 warn+log）。
    # HERO_REDIS_HOST 拼装
    host = (os.getenv("HERO_REDIS_HOST", "") or "").strip()
    if host:
        try:
            port_raw = (os.getenv("HERO_REDIS_PORT", "6379") or "6379").strip()
            port = int(port_raw) if port_raw else 6379
        except (ValueError, TypeError) as e:
            # 中文：非法 PORT 静默回退难排查，需告警
            warnings.warn(f"Invalid HERO_REDIS_PORT={port_raw!r}: {e}, using 6379", UserWarning, stacklevel=2)
            logger.warning("Invalid HERO_REDIS_PORT %r: %s, using 6379", port_raw, e)
            port = 6379
        if not 1 <= port <= 65535:
            # 中文：端口越界同样回退 6379 并告警，避免拼出非法 DSN
            warnings.warn(f"Invalid HERO_REDIS_PORT={port} out of range 1-65535, using 6379", UserWarning, stacklevel=2)
            logger.warning("Invalid HERO_REDIS_PORT %r out of range 1-65535, using 6379", port)
            port = 6379
        # PR-151 收尾：provider 优先，env 后端下才回退 HERO_REDIS_PASSWORD（同源 env）；
        # vault/kms 不通时不回落（pw 为空 → DSN 无口令，连接失败按既有语义暴露）。
        pw, _pw_reason = _redis_password_from_provider((os.getenv("HERO_REDIS_PASSWORD", "") or "").strip())
        try:
            db_raw = (os.getenv("HERO_REDIS_DB", "0") or "0").strip()
            db = int(db_raw) if db_raw else 0
        except (ValueError, TypeError) as e:
            # 中文：非法 DB 静默回退难排查，需告警
            warnings.warn(f"Invalid HERO_REDIS_DB={db_raw!r}: {e}, using 0", UserWarning, stacklevel=2)
            logger.warning("Invalid HERO_REDIS_DB %r: %s, using 0", db_raw, e)
            db = 0
        if db < 0:
            # 中文：负 DB 非法，回退 0 并告警
            warnings.warn(f"Invalid HERO_REDIS_DB={db} must be >=0, using 0", UserWarning, stacklevel=2)
            logger.warning("Invalid HERO_REDIS_DB %r must be >=0, using 0", db)
            db = 0
        # 中文：口令/主机做 URL 编码，含 @/:?# 的口令不再撕裂 DSN 解析
        auth = f":{_urlquote(pw, safe='')}@" if pw else ""
        host_q = _urlquote(host, safe=".-_:")
        return f"redis://{auth}{host_q}:{port}/{db}"
    # REDIS_URL 兼容
    alt = (os.getenv("REDIS_URL", "") or "").strip()
    if alt and alt.lower().startswith(("redis://", "rediss://")):
        return _apply_redis_password(alt)
    return None


_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off", "")


def _bool_from_env(key: str) -> bool:
    """布尔开关统一解析：1/true/yes/on 为真，0/false/no/off/空为假，缺省假。

    两个新开关（HERO_READY_COHERE_FATAL / HERO_BILLING_ALLOW_MEMORY）默认 false 的共同语义：
    fail-open 于可用性、fail-closed 于谎报 —— 不配即不启用宽松/降级路径（该报错仍显式报错，
    不会退化成假成功）；显式开启才允许 warning 放行或内存降级，并在响应里如实标注。
    非法值（如 maybe）告警后按 false 处理，保持 fail-visible 而非静默。
    """
    raw = (os.getenv(key, "") or "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    warnings.warn(f"Invalid {key}={raw!r}, treating as false", UserWarning, stacklevel=2)
    logger.warning("Invalid %s=%r, treating as false", key, raw)
    return False


def _llm_model_slot_from_env(key: str) -> str:
    """读取独立 LLM 槽位，未设置时回退到 legacy 模型配置；均做 strip 处理。"""
    raw = os.getenv(key, "")
    if raw and raw.strip():
        return raw.strip()
    legacy = os.getenv("HERO_LLM_MODEL", "")
    if legacy and legacy.strip():
        return legacy.strip()
    return "gpt-4o-mini"


_LLM_BASE_URL_SCHEMES = ("http", "https")


def _llm_base_url_from_env() -> str | None:
    """HERO_LLM_BASE_URL：OpenAI 兼容网关根地址（如 http://localhost:3001/v1）。

    - 空值 → None：不覆盖，走 SDK 官方默认端点；
    - 合法 → 去掉尾部 `/` 后返回（`http://h/v1/` 与 `http://h/v1` 等价）；
    - 非 http(s)（ftp: / javascript: / 无 scheme）或 netloc 内嵌 userinfo（`user:pass@host`）
      直接抛 ValueError —— 与其余严格字段一致：构造即失败，绝不把可疑网关带进运行态。
    错误信息不回显原始 URL（可能含旁路凭据），凭据只走 HERO_API_KEY。
    """
    raw = (os.getenv("HERO_LLM_BASE_URL", "") or "").strip()
    if not raw:
        return None
    scheme, sep, rest = raw.partition("://")
    scheme = scheme.strip().lower()
    if not sep or scheme not in _LLM_BASE_URL_SCHEMES:
        raise ValueError(f"HERO_LLM_BASE_URL must be an http(s) URL, got scheme {scheme or '<none>'!r}")
    netloc = rest.split("/", 1)[0]
    if "@" in netloc:
        raise ValueError("HERO_LLM_BASE_URL must not embed userinfo (user:pass@host); put credentials in HERO_API_KEY")
    if not netloc:
        raise ValueError("HERO_LLM_BASE_URL is missing a host")
    return f"{scheme}://{rest}".rstrip("/")


# ---- PR-151 密钥托管（B10-B）：env gate 唯一入口，secrets 包不得直读 os.getenv ----
# tests/test_config.py::test_no_raw_getenv_outside_config 锁定「src 下除 config 外无 os.getenv」，
# 故 secrets 包经下面两个函数取 env，散落的 os.getenv 不扩散到 src/hero_quant/secrets/。
_SECRETS_BACKENDS = ("env",)
# 已登记但本构建未实现：settings 层放行（配置合法），运行时由 get_secrets_provider 显式抛
# UnsupportedSecretsBackendError，绝不静默回落 env（明文 .env 不等于"已托管"）。
_SECRETS_RESERVED_BACKENDS = ("vault", "kms")
_DEFAULT_SECRETS_BACKEND = "env"


def secrets_backend_from_env() -> str:
    """HERO_SECRETS_BACKEND：密钥托管后端；默认 env；未知值直接 ValueError（fail-closed）。

    vault/kms 在本构建中登记但未实现：这里**不**抛（配置层面合法），由
    hero_quant.secrets.manager.get_secrets_provider 抛 UnsupportedSecretsBackendError，
    避免"配了 vault 却静默拿到 .env 明文值"的谎报。
    """
    raw = (os.getenv("HERO_SECRETS_BACKEND", "") or "").strip().lower()
    if not raw:
        return _DEFAULT_SECRETS_BACKEND
    if raw in _SECRETS_BACKENDS or raw in _SECRETS_RESERVED_BACKENDS:
        return raw
    raise ValueError(f"HERO_SECRETS_BACKEND must be one of {sorted(_SECRETS_BACKENDS + _SECRETS_RESERVED_BACKENDS)}, got {raw!r}")


def secret_env_value(env_var: str) -> str | None:
    """secrets 包读取 env 的唯一入口：空串视同未配置（返回 None，不造值）。

    不做 strip —— 密钥首尾空白是有意的字节，裁剪会静默改值。
    """
    raw = os.getenv(env_var, "")
    return raw or None


# 中文：W1 身份/配额 env 统一在此解析，auth 包不得直读 os.environ（便于审计与热更新）。
_AUTH_MODES = ("off", "enforce")
_DEFAULT_OIDC_ALGORITHMS = ("RS256",)
_KEYCLOAK_CERTS_PATH = "/protocol/openid-connect/certs"


def _auth_mode_from_env() -> str:
    """HERO_AUTH_MODE：off/enforce；未设或非法均收敛 enforce（安全默认），非法值告警。"""
    raw = (os.getenv("HERO_AUTH_MODE", "") or "").strip().lower()
    if raw in _AUTH_MODES:
        return raw
    if raw:
        warnings.warn(f"Invalid HERO_AUTH_MODE={raw!r}, treating as enforce", UserWarning, stacklevel=2)
        logger.warning("Invalid HERO_AUTH_MODE=%r, treating as enforce", raw)
    return "enforce"


def _oidc_jwks_url_from_env() -> str:
    """JWKS 地址：显式配置优先，否则按 Keycloak 约定从 issuer 推导。"""
    explicit = (os.getenv("HERO_OIDC_JWKS_URL", "") or "").strip()
    if explicit:
        return explicit
    issuer = (os.getenv("HERO_OIDC_ISSUER", "") or "").strip()
    if not issuer:
        return ""
    return issuer.rstrip("/") + _KEYCLOAK_CERTS_PATH


def _oidc_algorithms_from_env() -> tuple[str, ...]:
    """HERO_OIDC_ALGORITHMS：CSV 解析并大写；空值回退 RS256。"""
    raw = (os.getenv("HERO_OIDC_ALGORITHMS", "") or "").strip()
    if not raw:
        return _DEFAULT_OIDC_ALGORITHMS
    algorithms = tuple(part.strip().upper() for part in raw.split(",") if part.strip())
    return algorithms or _DEFAULT_OIDC_ALGORITHMS


def _bounded_int_from_env(key: str, default: int, *, low: int, high: int) -> int:
    """有界整数 env：缺失用默认；非法/越界告警后回默认（不把错配置带进运行态）。"""
    raw = (os.getenv(key, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        warnings.warn(f"Invalid {key}={raw!r}, using default {default}", UserWarning, stacklevel=2)
        logger.warning("Invalid %s=%r, using default %d", key, raw, default)
        return default
    if value < low or value > high:
        warnings.warn(f"{key}={value} out of range [{low}, {high}], using default {default}", UserWarning, stacklevel=2)
        logger.warning("%s=%d out of range [%d, %d], using default %d", key, value, low, high, default)
        return default
    return value


_DEFAULT_PIT_ROOT = "/root/hero-data/pit"


def _pit_root_from_env() -> str:
    """HERO_PIT_ROOT：PIT Parquet 存储根目录（仓库外，绝对路径）。

    - 空值 → 默认 /root/hero-data/pit（批次14 决策：数据落在仓库外，避免撑爆仓库所在分区）；
    - 必须是绝对路径且不含波浪号：expanduser 在 systemd / 容器里语义不同，一律拒绝；
    - 存在性/可写性**不在这里校验**：Settings 被大量进程与测试构造，缺目录不该让配置层崩。
      真正使用处（PitParquetStore）必须 fail-fast 抛错，绝不静默 mkdir。
    """
    raw = (os.getenv("HERO_PIT_ROOT", "") or "").strip()
    if not raw:
        return _DEFAULT_PIT_ROOT
    if "~" in raw or not raw.startswith("/"):
        raise ValueError("HERO_PIT_ROOT must be an absolute path without '~'")
    return raw.rstrip("/") or "/"


_PRICING_BACKENDS = ("null", "catalog")


def _pricing_backend_from_env() -> str:
    """HERO_PRICING_BACKEND：定价源选择（PR-112 接线，批次15）。

    - 默认 `catalog`：用仓内 llm/catalog.py 的价目（有价就 priced，目录外仍 unpriced）；
    - `null`/none/off：明确关闭定价，所有调用记 unpriced（不写 0 元）；
    - 其它值直接 ValueError：配置写错必须显式失败，不能静默当成 null 让上游以为拿到了价。

    与 HERO_SECRETS_BACKEND 同款 fail-closed 风格；三处联动见 .env.example 与 README env 表。
    """
    raw = (os.getenv("HERO_PRICING_BACKEND", "") or "").strip().lower()
    if not raw:
        return "catalog"
    if raw in ("null", "none", "off"):
        return "null"
    if raw == "catalog":
        return "catalog"
    raise ValueError(f"HERO_PRICING_BACKEND must be one of {_PRICING_BACKENDS}, got {raw!r}")


def _role_dsn_from_env(key: str) -> str | None:
    """按角色拆分的 DSN（批次15）：HERO_QUEUE_WORKER_DSN / HERO_ADMIN_READONLY_DSN。

    - 空值 → None（回落到 checkpoint DSN，保持单库单角色可用）；
    - 非 PostgreSQL scheme → ValueError（写错协议不许静默当没配）；
    - 口令仍走 provider 注入（与 _apply_pg_password 同规则，vault/kms 不通则不注入）。
    """
    raw = (os.getenv(key, "") or "").strip()
    if not raw:
        return None
    if not raw.lower().startswith(_PG_PREFIXES):
        raise ValueError(f"{key} must be a postgresql:// DSN")
    return _apply_pg_password(raw)


@dataclass
class Settings:
    """全局配置聚合，字段按分组：LLM / 数据与基准 / wall-time 治理 / 向量与嵌入 / checkpoint。"""

    llm_provider: str = field(default_factory=lambda: (os.getenv("HERO_LLM_PROVIDER", "") or "").strip() or "openai")
    llm_model: str = field(default_factory=lambda: (os.getenv("HERO_LLM_MODEL", "") or "").strip() or "gpt-4o-mini")
    llm_model_deep: str = field(default_factory=lambda: _llm_model_slot_from_env("HERO_LLM_MODEL_DEEP"))
    llm_model_quick: str = field(default_factory=lambda: _llm_model_slot_from_env("HERO_LLM_MODEL_QUICK"))
    api_key: str | None = field(default_factory=lambda: os.getenv("HERO_API_KEY"), repr=False)  # type: ignore[arg-type]
    # OpenAI 兼容网关根地址：空 = 不覆盖（官方 API）；仅 http/https，见 _llm_base_url_from_env
    llm_base_url: str | None = field(default_factory=_llm_base_url_from_env)
    data_default_market: str = field(default_factory=lambda: (os.getenv("HERO_DATA_MARKET", "") or "").strip() or "CN")
    data_mode: str = field(default_factory=lambda: (os.getenv("HERO_DATA_MODE", "") or "").strip() or "live")
    # PIT Parquet 根目录（仓库外绝对路径；见 _pit_root_from_env）
    pit_root: str = field(default_factory=_pit_root_from_env)
    # 定价源：catalog（默认，仓内价目）| null（明确关闭，全部 unpriced）
    pricing_backend: str = field(default_factory=_pricing_backend_from_env)
    # 按角色拆分的 DSN（批次15）：worker 用 hero_queue_worker、管理后台只读用 hero_admin_readonly；
    # 留空则回落 checkpoint_dsn（单角色部署仍可用，但跨租户能力取决于该角色的 RLS 策略）
    queue_worker_dsn: str | None = field(default_factory=lambda: _role_dsn_from_env("HERO_QUEUE_WORKER_DSN"))
    admin_readonly_dsn: str | None = field(default_factory=lambda: _role_dsn_from_env("HERO_ADMIN_READONLY_DSN"))
    # data_mode 默认 live（生产安全）：禁止 live 失败静默回退合成；仅当 HERO_DATA_MODE=synthetic 显式指定时允许合成
    # 基准指数映射：用于多市场回测时选择对照指数，默认覆盖常见后缀
    benchmark_ticker: str | None = field(default_factory=lambda: os.getenv("HERO_BENCHMARK_TICKER") or None)  # type: ignore[arg-type]
    benchmark_map: dict = field(
        default_factory=lambda: {
            ".NS": "^NSEI",
            ".BO": "^BSESN",
            ".T": "^N225",
            ".HK": "^HSI",
            ".L": "^FTSE",
            ".TO": "^GSPTSE",
            ".AX": "^AXJO",
            ".SS": "000001.SS",
            ".SZ": "399001.SZ",
            "": "SPY",
        }
    )
    # wall-time 预算（单位：秒），兼容两个环境变量键，None 表示不限制 — 单次解析防发散
    wall_time_budget_seconds: float | None = field(default_factory=_wall_time_budget_from_env)
    wall_time_budget: float | None = field(default=None)

    def __post_init__(self) -> None:
        # Alias wall_time_budget to wall_time_budget_seconds if not explicitly set, avoid double env parse divergence
        if self.wall_time_budget is None:
            self.wall_time_budget = self.wall_time_budget_seconds
        # 兜底 strip 其它字符串字段，保持与 _llm_model_slot_from_env 一致的 whitespace 净化
        for k in ("vector_store", "vector_enabled", "sbert_model", "openai_embed_model"):
            v = getattr(self, k, None)
            if isinstance(v, str):
                setattr(self, k, v.strip() or None if k in ("vector_store", "vector_enabled") else v.strip())
    # 向量/嵌入配置：集中在此 gate 解析，避免在 embed 模块直接读环境变量
    vector_dim: int = field(default_factory=_vector_dim_from_env)
    embed_provider: str = field(default_factory=_embed_provider_from_env)
    vector_store: str | None = field(default_factory=lambda: (os.getenv("HERO_VECTOR_STORE") or None))  # type: ignore[arg-type]
    vector_dsn: str | None = field(default_factory=_vector_dsn_from_env, repr=False)
    vector_enabled: str | None = field(default_factory=lambda: (os.getenv("HERO_VECTOR_ENABLED") or None))  # type: ignore[arg-type]
    # 嵌入模型细节：通过 gate 暴露，保持 embed 模块无直接环境读取
    sbert_model: str = field(default_factory=lambda: (os.getenv("HERO_SBERT_MODEL", "") or "").strip() or "all-MiniLM-L6-v2")
    openai_embed_model: str = field(default_factory=lambda: (os.getenv("HERO_OPENAI_EMBED_MODEL", "") or "").strip() or "text-embedding-3-small")
    openai_api_key: str | None = field(default_factory=lambda: os.getenv("OPENAI_API_KEY") or None, repr=False)  # type: ignore[arg-type]
    checkpoint_dsn: str | None = field(default_factory=_checkpoint_dsn_from_env, repr=False)
    checkpoint_ttl_seconds: int = field(default_factory=_checkpoint_ttl_from_env)
    billing_dsn: str | None = field(default_factory=_billing_dsn_from_env, repr=False)
    cohere_api_key: str = field(default_factory=lambda: os.getenv("COHERE_API_KEY", "") or "", repr=False)
    redis_dsn: str | None = field(default_factory=_redis_dsn_from_env, repr=False)
    # PII 加密/脱敏：统一在此 gate 解析，避免 api.middleware.pii 直接读环境变量（test_config 架构门禁校验）
    pii_encryption_key: str = field(default_factory=lambda: (os.getenv("PII_ENCRYPTION_KEY", "") or "").strip(), repr=False)
    pii_allow_legacy_noenc: bool = field(default_factory=lambda: (os.getenv("PII_ALLOW_LEGACY_NOENC", "") or "").strip() == "1")
    # PR-151 密钥托管后端：env（默认，明文 .env）/ vault / kms（登记未实现，运行时显式报错）。
    # 注意：本字段**不是**密钥读取通道，取密钥走 hero_quant.secrets.get_secrets_provider()。
    secrets_backend: str = field(default_factory=secrets_backend_from_env)
    # /ready 与 billing 的两个降级开关：统一经本网关解析，禁止调用方直读 os.environ。
    # 默认均为 false：fail-open 于可用性（缺 COHERE key 只 warning、/ready 不因此变红）、
    # fail-closed 于谎报（billing 未配 DSN 仍显式不可用，绝不静默降级成假成功）。
    ready_cohere_fatal: bool = field(default_factory=lambda: _bool_from_env("HERO_READY_COHERE_FATAL"))
    billing_allow_memory: bool = field(default_factory=lambda: _bool_from_env("HERO_BILLING_ALLOW_MEMORY"))
    # 身份/租户（W1）：OIDC 与配额 env 的唯一解析入口，auth 包只读本对象。
    # 未配 issuer/audience 时 auth_enabled() 为 false，开发环境不会因缺配置被强制鉴权打穿。
    auth_mode: str = field(default_factory=_auth_mode_from_env)
    oidc_issuer: str = field(default_factory=lambda: (os.getenv("HERO_OIDC_ISSUER", "") or "").strip())
    oidc_audience: str = field(default_factory=lambda: (os.getenv("HERO_OIDC_AUDIENCE", "") or "").strip())
    oidc_jwks_url: str = field(default_factory=_oidc_jwks_url_from_env)
    oidc_algorithms: tuple[str, ...] = field(default_factory=_oidc_algorithms_from_env)
    oidc_leeway_seconds: int = field(default_factory=lambda: _bounded_int_from_env("HERO_OIDC_LEEWAY_SECONDS", 60, low=0, high=600))
    oidc_jwks_ttl_seconds: int = field(default_factory=lambda: _bounded_int_from_env("HERO_OIDC_JWKS_TTL_SECONDS", 300, low=30, high=3600))
    tenant_requests_per_minute: int = field(default_factory=lambda: _bounded_int_from_env("HERO_TENANT_REQUESTS_PER_MINUTE", 60, low=1, high=1_000_000))
    tenant_daily_token_budget: int = field(default_factory=lambda: _bounded_int_from_env("HERO_TENANT_DAILY_TOKEN_BUDGET", 2_000_000, low=0, high=10**12))


@lru_cache(maxsize=1)
def _get_settings_cached() -> Settings:
    """进程内缓存的原始实例——仅内部持有，永不直接外泄（防可变成员污染全局）。"""
    return Settings()


def get_settings() -> Settings:
    """Cached factory：返回深拷贝隔离实例，调用方变更不污染全局；env 变更经 cache_clear 刷新。

    保留 lru_cache 语义（conftest/单测沿用 get_settings.cache_clear() 即可刷新）。
    """
    return copy.deepcopy(_get_settings_cached())


# 兼容：既有调用方/测试夹具沿用 get_settings.cache_clear() 刷新缓存。
get_settings.cache_clear = _get_settings_cached.cache_clear  # type: ignore[attr-defined]

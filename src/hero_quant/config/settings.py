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


def _checkpoint_dsn_from_env() -> str:
    """Checkpoint PG DSN：显式配置优先；未配置时回退无口令本地 PG 默认。

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
            return s
        warnings.warn(f"HERO_CHECKPOINT_DSN does not look like PG DSN: {_redact_dsn(s)!r}", UserWarning, stacklevel=2)
        logger.warning("HERO_CHECKPOINT_DSN invalid PG DSN: %r", _redact_dsn(s))
        # fall through to alias / fail-closed rather than returning garbage
    # also respect legacy HERO_PG_DSN alias (requires prefix consistently)
    alt = os.getenv("HERO_PG_DSN", "")
    if alt and alt.strip() and alt.strip().lower().startswith(_PG_PREFIXES):
        return alt.strip()
    elif alt and alt.strip():
        warnings.warn(f"HERO_PG_DSN does not look like PG DSN: {_redact_dsn(alt)!r}", UserWarning, stacklevel=2)
        logger.warning("HERO_PG_DSN invalid PG DSN: %r", _redact_dsn(alt))
    # 未配置：回退无口令本地 PG 默认（Task7 pinning 单测锁定 PG 形态；运行时无池走 emulated/memory）。
    # 注意：此默认不含任何口令；fail-closed None 需先更新 pinning 单测（open item）。
    return "postgresql://localhost:5432/hero_quant"


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
            return raw
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
            return raw.strip()
    return None


def _redis_dsn_from_env() -> str | None:
    """Redis DSN 解析，遵循单一 env gate 约束。

    优先级：HERO_REDIS_DSN（含 redis:// 前缀校验） > HERO_REDIS_HOST/PORT/PASSWORD/DB 拼装 > REDIS_URL 兼容。
    返回 None 表示未配置（调用方可回退 fakeredis 或 fail-closed）。
    """
    raw = os.getenv("HERO_REDIS_DSN", "")
    if raw and raw.strip():
        s = raw.strip()
        if s.lower().startswith(("redis://", "rediss://", "unix://")):
            return s
        warnings.warn(f"HERO_REDIS_DSN does not look like redis DSN: {_redact_dsn(s)!r}", UserWarning, stacklevel=2)
        logger.warning("HERO_REDIS_DSN invalid redis DSN: %r", _redact_dsn(s))
        return None  # fail-visible：调用方走 HERO_REDIS_HOST/REDIS_URL 拼装或 fakeredis/fail-closed，不传垃圾给 redis 库
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
        pw = (os.getenv("HERO_REDIS_PASSWORD", "") or "").strip()
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
        return alt
    return None


def _llm_model_slot_from_env(key: str) -> str:
    """读取独立 LLM 槽位，未设置时回退到 legacy 模型配置；均做 strip 处理。"""
    raw = os.getenv(key, "")
    if raw and raw.strip():
        return raw.strip()
    legacy = os.getenv("HERO_LLM_MODEL", "")
    if legacy and legacy.strip():
        return legacy.strip()
    return "gpt-4o-mini"


@dataclass
class Settings:
    """全局配置聚合，字段按分组：LLM / 数据与基准 / wall-time 治理 / 向量与嵌入 / checkpoint。"""

    llm_provider: str = field(default_factory=lambda: (os.getenv("HERO_LLM_PROVIDER", "") or "").strip() or "openai")
    llm_model: str = field(default_factory=lambda: (os.getenv("HERO_LLM_MODEL", "") or "").strip() or "gpt-4o-mini")
    llm_model_deep: str = field(default_factory=lambda: _llm_model_slot_from_env("HERO_LLM_MODEL_DEEP"))
    llm_model_quick: str = field(default_factory=lambda: _llm_model_slot_from_env("HERO_LLM_MODEL_QUICK"))
    api_key: str | None = field(default_factory=lambda: os.getenv("HERO_API_KEY"), repr=False)  # type: ignore[arg-type]
    data_default_market: str = field(default_factory=lambda: (os.getenv("HERO_DATA_MARKET", "") or "").strip() or "CN")
    data_mode: str = field(default_factory=lambda: (os.getenv("HERO_DATA_MODE", "") or "").strip() or "live")
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

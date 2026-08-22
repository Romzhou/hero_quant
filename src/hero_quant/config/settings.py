"""配置聚合入口 —— 唯一环境变量网关（env gate）。

职责：集中解析 HERO_* 环境变量并暴露为 Settings 数据类；架构位置：config 层最底层，其余模块仅依赖 Settings。
设计约定：所有 HERO_* 映射在此文件通过 os.getenv 完成，避免分散读取；支持 HERO_WALL_TIME_BUDGET_SECONDS 等别名的兼容解析。
"""

import os
from dataclasses import dataclass, field


def _wall_time_budget_from_env() -> float | None:
    """解析 wall-time 预算（单位：秒），兼容两个环境变量键，仅接受正数。"""
    for key in ("HERO_WALL_TIME_BUDGET_SECONDS", "HERO_WALL_TIME_BUDGET"):
        raw = os.getenv(key, "")
        if raw and str(raw).strip():
            try:
                v = float(str(raw).strip())
                if v > 0:
                    return v
            except Exception:
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
            except Exception:
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
            if s.startswith(_PG_PREFIXES):
                return s
    store = (os.getenv("HERO_VECTOR_STORE", "") or "").strip().lower()
    if store in ("pgvector", "postgres", "pg", "auto"):
        c = (os.getenv("HERO_CHECKPOINT_DSN", "") or "").strip()
        if c.startswith(_PG_PREFIXES):
            return c
    if (os.getenv("HERO_VECTOR_ENABLED", "") or "").strip().lower() in ("1", "true", "yes", "pgvector", "on"):
        c = (os.getenv("HERO_CHECKPOINT_DSN", "") or "").strip()
        if c.startswith(_PG_PREFIXES):
            return c
    return None


def _llm_model_slot_from_env(key: str) -> str:
    """读取独立 LLM 槽位，未设置时回退到 legacy 模型配置。"""
    return os.getenv(key) or os.getenv("HERO_LLM_MODEL", "gpt-4o-mini")


@dataclass
class Settings:
    """全局配置聚合，字段按分组：LLM / 数据与基准 / wall-time 治理 / 向量与嵌入 / checkpoint。"""

    llm_provider: str = field(default_factory=lambda: os.getenv("HERO_LLM_PROVIDER", "openai"))
    llm_model: str = field(default_factory=lambda: os.getenv("HERO_LLM_MODEL", "gpt-4o-mini"))
    llm_model_deep: str = field(default_factory=lambda: _llm_model_slot_from_env("HERO_LLM_MODEL_DEEP"))
    llm_model_quick: str = field(default_factory=lambda: _llm_model_slot_from_env("HERO_LLM_MODEL_QUICK"))
    api_key: str | None = field(default_factory=lambda: os.getenv("HERO_API_KEY"))  # type: ignore[arg-type]
    data_default_market: str = field(default_factory=lambda: os.getenv("HERO_DATA_MARKET", "CN"))
    data_mode: str = field(default_factory=lambda: os.getenv("HERO_DATA_MODE", "synthetic"))
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
    # wall-time 预算（单位：秒），兼容两个环境变量键，None 表示不限制
    wall_time_budget_seconds: float | None = field(default_factory=_wall_time_budget_from_env)
    wall_time_budget: float | None = field(default_factory=_wall_time_budget_from_env)
    # 向量/嵌入配置：集中在此 gate 解析，避免在 embed 模块直接读环境变量
    vector_dim: int = field(default_factory=_vector_dim_from_env)
    embed_provider: str = field(default_factory=_embed_provider_from_env)
    vector_store: str | None = field(default_factory=lambda: os.getenv("HERO_VECTOR_STORE") or None)  # type: ignore[arg-type]
    vector_dsn: str | None = field(default_factory=_vector_dsn_from_env)
    vector_enabled: str | None = field(default_factory=lambda: os.getenv("HERO_VECTOR_ENABLED") or None)  # type: ignore[arg-type]
    # 嵌入模型细节：通过 gate 暴露，保持 embed 模块无直接环境读取
    sbert_model: str = field(default_factory=lambda: os.getenv("HERO_SBERT_MODEL", "all-MiniLM-L6-v2"))
    openai_embed_model: str = field(default_factory=lambda: os.getenv("HERO_OPENAI_EMBED_MODEL", "text-embedding-3-small"))
    openai_api_key: str | None = field(default_factory=lambda: os.getenv("OPENAI_API_KEY") or None)  # type: ignore[arg-type]
    checkpoint_dsn: str = field(default_factory=lambda: os.getenv("HERO_CHECKPOINT_DSN", "memory://default"))

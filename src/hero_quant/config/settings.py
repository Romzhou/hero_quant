"""Single env entry for hero-quant.

Only this file is allowed to call os.getenv (env gate).
All other src modules must import Settings instead of using raw getenv.
"""

import os
from dataclasses import dataclass, field


def _wall_time_budget_from_env() -> float | None:
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


# ---- vector / embed env helpers (single env gate) ----
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


@dataclass
class Settings:
    llm_provider: str = field(default_factory=lambda: os.getenv("HERO_LLM_PROVIDER", "openai"))
    llm_model: str = field(default_factory=lambda: os.getenv("HERO_LLM_MODEL", "gpt-4o-mini"))
    api_key: str | None = field(default_factory=lambda: os.getenv("HERO_API_KEY"))  # type: ignore[arg-type]
    data_default_market: str = field(default_factory=lambda: os.getenv("HERO_DATA_MARKET", "CN"))
    data_mode: str = field(default_factory=lambda: os.getenv("HERO_DATA_MODE", "synthetic"))
    # Benchmark — mirrors TradingAgents default_config.py:152 benchmark_map
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
    # Wall-time governance — budget in seconds (HERO_WALL_TIME_BUDGET / HERO_WALL_TIME_BUDGET_SECONDS)
    wall_time_budget_seconds: float | None = field(default_factory=_wall_time_budget_from_env)
    wall_time_budget: float | None = field(default_factory=_wall_time_budget_from_env)
    # Vector / embedding — consolidated env gate (Task 12 MUST)
    vector_dim: int = field(default_factory=_vector_dim_from_env)
    embed_provider: str = field(default_factory=_embed_provider_from_env)
    vector_store: str | None = field(default_factory=lambda: os.getenv("HERO_VECTOR_STORE") or None)  # type: ignore[arg-type]
    vector_dsn: str | None = field(default_factory=_vector_dsn_from_env)
    vector_enabled: str | None = field(default_factory=lambda: os.getenv("HERO_VECTOR_ENABLED") or None)  # type: ignore[arg-type]
    # Embed model details (also via gate to avoid raw os.environ in embed.py)
    sbert_model: str = field(default_factory=lambda: os.getenv("HERO_SBERT_MODEL", "all-MiniLM-L6-v2"))
    openai_embed_model: str = field(default_factory=lambda: os.getenv("HERO_OPENAI_EMBED_MODEL", "text-embedding-3-small"))
    openai_api_key: str | None = field(default_factory=lambda: os.getenv("OPENAI_API_KEY") or None)  # type: ignore[arg-type]
    checkpoint_dsn: str = field(default_factory=lambda: os.getenv("HERO_CHECKPOINT_DSN", "memory://default"))

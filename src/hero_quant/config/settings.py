"""Single env entry for hero-quant.

Only this file is allowed to call os.getenv (env gate).
All other src modules must import Settings instead of using raw getenv.
"""

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    llm_provider: str = field(default_factory=lambda: os.getenv("HERO_LLM_PROVIDER", "openai"))
    llm_model: str = field(default_factory=lambda: os.getenv("HERO_LLM_MODEL", "gpt-4o-mini"))
    api_key: str | None = field(default_factory=lambda: os.getenv("HERO_API_KEY"))  # type: ignore[arg-type]
    data_default_market: str = field(default_factory=lambda: os.getenv("HERO_DATA_MARKET", "CN"))

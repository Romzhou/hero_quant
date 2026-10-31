"""Risk 归因与影子账户重放（PR-214）。

只做一件事：把回测产物（positions / prices / metrics）换算成可复核的
收益归因与影子风控重放结果；算不出来就 null + degraded + reason，绝不填占位值。
"""

from __future__ import annotations

from hero_quant.risk.attribution import (  # noqa: F401
    ATTRIBUTION_KEYS,
    build_shadow_replay,
    compute_attribution,
    default_cost_rate,
    load_positions,
    load_prices,
)

__all__ = [
    "ATTRIBUTION_KEYS",
    "build_shadow_replay",
    "compute_attribution",
    "default_cost_rate",
    "load_positions",
    "load_prices",
]

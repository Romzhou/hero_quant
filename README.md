# 真英雄量化 (hero-quant)

> 绿地新项目，批判性借鉴 [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 的 8 大模式：AgentLoop 状态机 / 上下文折叠 / Grounding 证据账本 / Tool 自动发现 / 行情 Fallback+Provenance / 回测引擎 / 治理 Hash 链 / 供应链契约。

**设计**: `D:\kaipanla-data\vibe-trading\docs\plans\2026-08-20-slim-research-design.md`  
**TDD 计划**: `D:\kaipanla-data\vibe-trading\docs\plans\2026-08-20-hero-quant-plan.md` (18 tasks)

## 快速开始

```bash
pip install -e ".[dev,ashare,us]"
pytest -q
```

## 架构

- `src/hero_quant/agent/` — loop / context / grounding / trace
- `src/hero_quant/tools/` — registry 自动发现
- `src/hero_quant/data/` — registry + loaders (tencent/yahoo 可插拔)
- `src/hero_quant/backtest/` — engine + metrics + validation
- `src/hero_quant/memory/` — file + FTS5
- `src/hero_quant/governance/` — hash 链
- `src/hero_quant/api/` — FastAPI + SSE + /metrics
- `frontend/` — React 19 三页 (Chat/Research/Settings)
```


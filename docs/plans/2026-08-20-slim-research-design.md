# 极简投研 Agent 新项目设计 — 批判性借鉴 Vibe-Trading

> **日期**: 2026-08-20 | **状态**: Approved (7节逐节确认) | **发起人**: 用户（选型 A→ 绿地新写）  
> **定位**: 新开独立项目，**学习 Vibe-Trading 的工程与 Agent 架构**，其余绿地重写。非原仓瘦身。
> **已确认约束**: 全市场可插拔 + 保留极简前端(3页) + 全能力可插拔但默认轻量（Swarm/Live/Channels全保留为可选插件）

---

## 1. 目标与非目标

### 1.1 目标
1.  **可学习的极简内核**: 200-300个py文件跑通“自然语言→行情→回测→报告”闭环，单人2周可读完。
2.  **全市场可插拔**: loader/engines 通过 registry 注册，A股/美股/加密按需 `pip install my-agent[ashare]`，未装时给出可操作提示而非静默降级。
3.  **极简但完整的前端**: Chat / Research / Settings 三页，体验闭环但代码量 -70%。
4.  **为长期演进打桩**: 先单仓单包跑通，预留 Phase2 拆 `core` + `extensions` 双包的能力。

### 1.2 非目标
- 不原地重构 Vibe-Trading 仓（新仓绿地）。
- 不重写已被验证的回测数学（Quantlib类能力复用思路，但实现自写）。
- 不做微服务拆分（单进程 + 可选后台线程足够）。

### 1.3 原则
- **YAGNI**: 无真实用户前不做抽象。
- **Lazy + Fail-loud**: 重依赖全部惰性导入，缺失时抛 `ImportError("pip install my-agent[xxx]")`，绝不静默回退。
- **证据优于提示词**: 防幻觉靠 Grounding 对账，不靠“请不要编造”提示词。
- **注释即决策记录**: 每个安全/正确性边界写明“防什么、没防什么、实测残余”。

---

## 2. 对 Vibe-Trading 的批判性借鉴

### 2.1 必学（照搬思路，自写实现）

| 模式 | 来源 | 新项目落地 |
|------|------|------------|
| **AgentLoop 状态机** | `agent/src/agent/loop.py` — while+状态机+8控制点+2条非常规继续 | `src/agent/loop.py` 精简版：保留 run/terminate/recovery 三态，砍掉5层压缩中的2层，保留消息边界折叠 |
| **上下文分层与截断明示** | `context.py` + `trace.py` sidecar原子落盘 | 三层即可：system/ history(折叠) / tool-scratch；超阈值 sidecar + 截断横幅，绝不静默丢 |
| **Grounding 三防线** | `grounding.py` | 预取最近N天OHLCV为权威块 + 数字必溯源 + 输出契约检测（mock/fabricated拒收） |
| **工具自动发现** | `src/tools/` 64工具 + `skills/` 渐进披露 | `src/tools/registry.py` + `@tool` 装饰器自动注册；skills 按需 `load_skill`，描述精简控 token |
| **行情 Fallback + Provenance** | `market_data.py` + `backtest/loaders/registry.py` | `src/data/registry.py`，loader声明 market/unit/interval，provenance记录实际源与单位 |
| **回测引擎多市场抽象** | `backtest/engines/` | `src/backtest/engine.py` 抽象 + 各市场引擎懒加载，PIT/除权/费用在边界处理 |
| **治理账本思路** | `governance/ledger.py` hash链 | 轻量版 `src/governance/ledger.py`：seq+prev_hash 链 + 0600落盘，Phase1不做跨段验证 |
| **供应链契约** | `requirements-lock.txt` + digest-pin + CI hash校验 | 新仓即上 `uv pip compile --generate-hashes` + `pip install --require-hashes` + dependabot分组 |

### 2.2 必弃/必改

| 坑 | Vibe现状 | 新项目做法 |
|----|----------|------------|
| **过重** | 1498个py, 69直接依赖, 3阶段Docker | base 25依赖，frontend按需，重库全入extras |
| **Swarm仅单向** | DAG单向摘要，无双向信箱，`inboxes/`死代码 | Phase1不做Swarm；Phase2若做则引入结构化 `TaskOutput(JSON Schema)` + 信箱消费 |
| **可观测缺口** | 无 /metrics、无结构化日志、无Sentry | 首日即加 `/metrics` (prometheus_client) + structlog JSON + trace_id |
| **覆盖率无红线** | `fail_under=0` | `core` 模块 `fail_under=30` 起步，逐提 |
| **Grounding一次性** | 长run陈旧 | 预留 `refresh_grounding()` 钩子，Phase1不实现但接口先占 |

---

## 3. 总体架构（新仓）

```
my-trading-agent/
├── src/
│   ├── agent/        # loop, context, grounding, skills, trace
│   ├── tools/        # registry, 15-20个核心tool（market_data, backtest, quantlib_call等）
│   ├── data/         # registry + loaders/ (ashare/us/crypto 各一，默认只装一个)
│   ├── backtest/     # engine抽象 + loaders + metrics + validation
│   ├── memory/       # 3层记忆：session/file/vector(可选)，FTS5 + 去重窗口
│   ├── governance/   # ledger + audit（轻量）
│   ├── api/          # FastAPI + SSE + security
│   └── config/       # 唯一 env 读取处（CI gate 强制）
├── frontend/         # Vite + React + Zustand，3页
├── tests/            # pytest + vitest
└── pyproject.toml    # base + extras
```

**分层**: `agent` 不依赖 `data/backtest/memory` 具体实现，只依赖其抽象/registry；`api` 组装所有层。

---

## 4. 详细设计

### 4.1 AgentLoop（精简但保状态机）
- 循环：`while not terminated: llm.stream_chat → tool_calls → execute → grounding_check → context_compact`
- 终止：`max_iterations / token_limit / user_stop / tool_success + grounding_pass` 四条件
- 截断：消息边界折叠 + 迭代式摘要（抄 Vibe 2026-08-11修复后的做法）
- 可观测：每轮写 `trace.jsonl` (flush+fsync)，大字段走 sidecar

### 4.2 工具系统
- `@tool(name, description, schema)` 自动注册到 `TOOL_REGISTRY`
- 核心 tool 清单（15个）：`search_symbol`, `get_market_data`, `get_fundamentals`, `technical_indicators`, `run_backtest`, `quantlib_call`, `read_file`, `write_file`, `web_search`, `report_audit`, `remember`, `load_skill`, `list_skills`, `get_run_result`, `propose_mandate`(paper模式)
- MCP：Phase1不做MCP Server，仅保留 `src/tools/mcp.py` 客户端适配器占位

### 4.3 数据层可插拔
```python
# src/data/registry.py
registry.register(LoaderSpec(name="tencent", markets=["CN"], units="board_lots", intervals=["1d","1h"]))
registry.register(LoaderSpec(name="yahoo", markets=["US"], units="shares", intervals=["1d"]))
# 调用
bars, provenance = registry.get_bars(symbol, interval, start, end) # 内部逐loader fallback，记录实际源
```
- 未装 extra 时：`raise ImportError("A-share需 pip install my-agent[ashare]")`
- 单位声明 + provenance，杜绝 100倍跳变类bug。

### 4.4 回测层
- `Engine.run(targets, start, end, costs, constraints) -> positions.csv, fills.csv, metrics.json, tearsheet.html`
- 校验：`validation.py` 拒绝未来函数（PIT）、非正价格、混币种聚合
- 指标：`metrics.py` 复用 Vibe 的夏普/回撤/換手思路，自写实现

### 4.5 记忆（可选Phase1）
- Tier1: session内存，Tier2: `~/.my-agent/memory/*.md` 文件，Tier3: 向量（Phase2）
- 检索：FTS5 优先 + token扫描回退，CJK兼容（Vibe的无分词器+bigram思路可抄）
- 写入：30秒去重窗口 + flock + 原子写

### 4.6 Swarm/Live/Channels（全保留为插件）
- 代码存在但默认不加载：`VIBE_ENABLE_SWARM=0` 时不 import `src/swarm/`，路由 404
- `pyproject.toml: [project.optional-dependencies] swarm = ["..."], live = [...], channels = [...]`
- Live 默认 paper 模式，真钱路径需 `propose_mandate → consent → audit` 三步，缺一不可

---

## 5. 前端瘦身

- **3页**: 
  1. `Chat` — 对话 + 工具轨迹 + 流式思考
  2. `Research` — 回测报告（权益曲线+月度热力+回撤topN）+ 持仓
  3. `Settings` — Provider/模型/数据源开关 + extra安装提示
- 路由懒加载，ECharts/Recharts 按需 import，`style-src` 尽量不 `unsafe-inline`
- Desktop/Electron 移入 `extra[desktop]`，默认不装

---

## 6. 工程化与可观测（首日即做）

| 项 | 做法 |
|----|------|
| 供应链 | `uv pip compile --generate-hashes` → `requirements-lock.txt`，Docker `pip install --require-hashes`，Actions pin到SHA |
| CI | hash-lock三平台校验 + pytest --cov + pip-audit + gitleaks + ruff/black |
| 指标 | `prometheus_client` 暴露 `/metrics`: `request_latency`, `llm_tokens`, `rate_limit_hits` |
| 日志 | structlog JSON + `trace_id` 贯穿（request_id → agent run_id → tool call） |
| 审计 | `governance/ledger.py` 轻量hash链，0700权限，fsync |
| 安全 | `security/scanner.py` 抄Vibe的控制符中和（ChatML/全宽竖线），`api/security.py` 的HMAC+Host白名单 |

---

## 7. 技术选型（新仓）

- **后端**: Python 3.11, FastAPI, Pydantic v2, LangChain 1.x（仅作传输层，自研编排）, DuckDB(可选)
- **前端**: Node 22, React 19, Vite, Zustand, Tailwind
- **数据**: pandas, numpy, scipy, bottleneck, tushare(可选), yfinance(可选), ccxt(可选)
- **LLM**: OpenAI兼容 + Anthropic分支，provider适配器单文件

---

## 8. 实施路线图

### Phase 1: 内核闭环（1-2周，单包）
- [ ] 初始化 `my-agent` 空仓 + `uv` + `ruff/black/pytest`
- [ ] 实现 `agent/loop + context + grounding + trace`
- [ ] 实现 `tools/registry` + 15核心tool
- [ ] 实现 `data/registry` + 2个loader（tencent + yahoo）
- [ ] 实现 `backtest/engine` + metrics/validation
- [ ] 实现 `api/server` (FastAPI + SSE + /metrics)
- [ ] 实现 `frontend` 3页
- [ ] CI + hash-lock + pip-audit

### Phase 2: 插件化与可观测（2-3周，双包预备）
- [ ] 抽 `vibe-core` 逻辑分层，加 `entry_points` 插件发现
- [ ] 补 memory Tier2 + FTS5
- [ ] 补 governance hash链跨段验证
- [ ] 若需要：Swarm DAG按需启用（结构化输出+信箱）
- [ ] 覆盖率对 core 提至 60%，加 SBOM

---

## 9. 风险与取舍

| 风险 | 缓解 |
|------|------|
| 绿地重写低估工作量 | Phase1严格YAGNI，只做15 tool+2 loader，先跑通再扩展 |
| 回测正确性陷阱多 | 抄 Vibe 的 8月集中修复清单：PIT、除权、单位、时区、费用，逐项加回归测试 |
| LLM成本失控 | token>60k熔断 + 工具调用≤20 + microcompact(仅留近3条结果) |
| 插件化过度设计 | Phase1仅逻辑分层不物理拆包，用 lazy import 即可验证 |

---

## 附：与 Vibe-Trading 的文件级对照

| 新项目 | 对应Vibe | 借鉴点 |
|--------|----------|--------|
| `src/agent/loop.py` | `agent/src/agent/loop.py` | 状态机与终止条件 |
| `src/agent/context.py` | `agent/src/agent/context.py` | 折叠与截断明示 |
| `src/data/registry.py` | `agent/src/market_data.py` + `backtest/loaders/registry.py` | fallback+provenance |
| `src/tools/registry.py` | `agent/src/tools/` | 自动发现 |
| `src/governance/` | `agent/src/governance/` | hash链思路 |
| `frontend/src/pages/Chat.tsx` | `frontend/src/pages/` | 仅3页子集 |

---

> 下一步：确认本设计后，进入 **Phase 2: Writing Plans** — 产出 `docs/plans/2026-08-20-slim-research-plan.md`（task-by-task，TDD：test→fail→implement→pass→commit），再进 Subagent-Driven Build。

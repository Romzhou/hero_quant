# TradingAgents 深度剖析与 hero-quant 改进路标

> **Date:** 2026-08-21  
> **Scope:** TradingAgents v0.3.1 全量剖析 · 对照 vibetrading / deepseek-harness / hero-quant 同套路复盘  
> **Objective:** 提取可复用亮点 · 暴露结构性缺点 · 给出 hero-quant P0/P1/P2 改进任务  
> **Method:** 4 lanes 并行探查（TradingAgents / hero-quant / vibe-trading 0.1.13 / deepseek-harness 0.1.0-rc.5）+ 交叉验证  
> **Status:** Phase 1 Brainstorm 完成，待评审后进入 Phase 2 writing-plans

---

## 0. 结论先行（Executive Summary）

**一句话定位：** TradingAgents 是“**多智能体辩论驱动的研究型交易脚手架**”，用 LangGraph 把投行的角色分工（Analyst → Researcher → Trader → Risk → Portfolio Manager）硬编码为状态图；而 hero-quant 定位是“**单智能体 NL→行情→回测→报告闭环**”的工程化交易内核（PIT、资金缩放、tearsheet、Ledger）。vibe-trading 是“**正确性优先、治理厚重的全能型量化工作台**”（278k LOC、9 市场引擎、审计链）；deepseek-harness(dsh) 是“**Everything-is-Plugin 的通用 Agent 底座**”（Cordis 事件模型，50+ packages）。

**给 hero-quant 的 3 个核心启示：**

1. **辩论机制值得“轻量借鉴”，但不可照搬串行链** —— Bull/Bear/ResearchManager 的对抗式提炼确实提升研报深度，但 TradingAgents 的 `Market→Sentiment→News→Fundamentals` 顺序链 + `2*max_debate_rounds` + `3*max_risk_discuss_rounds` 导致单次 10+ LLM 调用、成本 $30/1M token，延迟与不确定性不可接受。hero-quant 应做 `Plan→Fanout(3 analyst 并行, Send) → Verify` 的 DAG 版本，保留辩论的“对抗验证”思想，去掉“真人投行cosplay”冗余。
2. **数据契约与幻觉治理是 TradingAgents 最值得抄的工程** —— `resolve_instrument_identity` + `build_instrument_context` 根治“张冠李戴”幻觉、`get_verified_market_snapshot` 校验价位、`NO_EXTERNAL_TOOLS` 约束结构化输出、`VENDOR_METHODS` 显式路由无静默 fallback。hero-quant 已有 1% CrossSource 阻断和 Grounding 三级校验，可叠加“身份锚定 + 校验快照”层。
3. **TradingAgents 的短板恰是 hero-quant 的护城河** —— 无真正回测引擎（列了 backtrader 却未接线）、Memory 单文件 markdown 膨胀、新闻“未来泄露”无法做历史回测、串行瓶颈；而 hero-quant 已具备 PIT 正逻辑 `weights_on≤price_date`、次日开盘执行、比例缩放、tearsheet/drawdown episodes/每月 ME 热力，这些要继续放大，并把 TradingAgents 缺的“批量回测评估、Sharpe/turnover 成本、alpha vs 区域基准”补齐做标准化 Bench。

**裁决：** 不需要复刻 TradingAgents；**抽其辩论与治理之长，补其回测与并发之短**，以 30k LOC 打 278k 的 B 策略（微内核 + Trait + Rust 60 算子）继续有效。

---

## 1. TradingAgents 全景图

### 1.1 目录与关键文件（2-3 层）

```
TradingAgents/ v0.3.1 (py>=3.10, setuptools)
├── README.md / CHANGELOG.md / Dockerfile / docker-compose.yml (ollama profile)
├── pyproject.toml (langgraph>=0.4.8, langchain-*, yfinance, backtrader *, pandas, stockstats, rich, typer)
├── main.py (TradingAgentsGraph.propagate NVDA demo) / cli/main.py (Rich Typer 8-step wizard)
├── tradingagents/
│   ├── default_config.py (DEFAULT_CONFIG + TRADINGAGENTS_* 20+ env 覆盖 + _coerce 类型强校验)
│   ├── graph/
│   │   ├── trading_graph.py (TradingAgentsGraph: 双 LLM deep/quick + ToolNode + Propagator + Reflector + SignalProcessor)
│   │   ├── setup.py (StateGraph: Analyst 链 → Bull↔Bear → ResearchManager → Trader → Agg/Cons/Neu → PortfolioManager)
│   │   ├── propagation.py (create_initial_state(AgentState) + get_graph_args)
│   │   ├── conditional_logic.py (should_continue_* / debate 2*N / risk 3*N)
│   │   ├── checkpointer.py (per-ticker SqliteSaver, thread_id=hash(ticker:date:signature))
│   │   ├── reflection.py (5日 realized + alpha vs benchmark → 1段 reflection LLM)
│   │   ├── signal_processing.py (markdown→rating/action)
│   │   └── analyst_execution.py (执行计划 + wall-time)
│   ├── agents/
│   │   ├── analysts/ market/sentiment/news/fundamentals (各绑 tool + 提示词)
│   │   ├── researchers/ bull/bear (quick_think, InvestDebateState.history)
│   │   ├── managers/ research_manager (deep 5-tier) / trader/ trader (quick 3-tier)
│   │   ├── risk_mgmt/ aggressive/conservative/neutral + portfolio_manager (deep 5-tier)
│   │   ├── utils/ agent_states.py (TypedDict), agent_utils.py (instrument 锚定 LRU256), memory.py (markdown log), structured.py (bind_structured fallback), schemas.py (Pydantic)
│   │   └── ... core_stock_tools / technical_indicators / fundamental / news / macro / prediction / market_data_validation
│   ├── dataflows/
│   │   ├── interface.py (VENDOR_METHODS 注册表 + route_to_vendor 显式链，无静默 fallback，OPTIONAL_CATEGORIES sentinel)
│   │   ├── config.py (set_config/get_config deepmerge, category>tool 优先级)
│   │   └── y_finance / alpha_vantage_* / fred / polymarket / stocktwits / reddit / symbol_utils / market_data_validator
│   ├── llm_clients/ factory.py + model_catalog.py + base_client + openai/anthropic/google/azure/bedrock + capabilities/validators/api_key_env
│   └── reporting.py (write_report_tree)
├── cli/ main.py + utils.py (ticker/date/language/analysts/depth/provider thinking) + models.py + config.py + stats_handler.py
├── tests/ 50+ (checkpoint/vendor_routing/memory_log/structured_agents/i18n/ticker_symbol)
└── assets/ schema.png / analyst/researcher/trader/risk.png / cli/*.png
```

*来源：`pyproject.toml:1` `default_config.py:10-28` `graph/setup.py` `graph/trading_graph.py` `dataflows/interface.py` `agents/utils/agent_utils.py`*

### 1.2 状态图（LangGraph）

```
[START]
  → Market Analyst ─┬─tool → get_stock_data / get_indicators / get_verified_market_snapshot (8 指标去冗余+表)
  │                 └─clear → MsgDelete (RemoveMessage + "Proceed... [ticker] on [date]") 
  → Sentiment Analyst ─┬─tool → get_news(Yahoo/StockTwits/Reddit) → SentimentReport structured
  → News Analyst ─┬─tool → get_news / get_global_news / get_insider_transactions / get_macro_indicators(FRED) / get_prediction_markets(Polymarket)
  → Fundamentals Analyst ─┬─tool → get_fundamentals / balance / cashflow / income
  → Bull ↔ Bear (should_continue_debate: count≥2*max_debate_rounds → ResearchManager else ping-pong, DEBATE_PATH_MAP crash-safe)
  → Research Manager (deep LLM, ResearchPlan 5-tier Buy/Overweight/Hold/Underweight/Sell + rationale + strategic_actions)
  → Trader (quick LLM, TraderProposal 3-tier Buy/Hold/Sell + entry/stop/sizing + "FINAL TRANSACTION PROPOSAL:" 行)
  → Aggressive → Conservative → Neutral (round-robin, count≥3*max_risk_discuss_rounds → PortfolioManager, RISK_ANALYSIS_PATH_MAP)
  → Portfolio Manager (deep LLM, PortfolioDecision 5-tier + executive_summary/thesis/price/time)
[END]

State: AgentState(MessagesState + company_of_interest / asset_type stock|crypto / instrument_context / trade_date / market|sentiment|news|fundamentals_report / investment_debate_state / investment_plan / trader_investment_plan / risk_debate_state / final_trade_decision / past_context)
```

### 1.3 编排与持久化

- `TradingAgentsGraph.__init__` 构建双 LLM（`deep_think` 推理 + `quick_think` 分析/辩论）via `create_llm_client(provider, model, backend_url, temperature, max_retries, thinking_level/reasoning_effort/effort)` + 每类 ToolNode + `GraphSetup` + `Propagator` + `Reflector` + `SignalProcessor`。`llm_max_retries` 可调扛 429。
- `propagate(ticker, date, asset_type)`：`_resolve_pending_entries`（5 日 raw/alpha vs benchmark via yfinance + reflector 1 段反思）→ 可选 `SqliteSaver` 每 ticker 一库 `~/.tradingagents/cache/checkpoints/<TICKER>.db`，`thread_id=hash(ticker:date:signature)`，`signature=analysts=...|debate=N|risk=N|asset=...` 感知图形状失效 → `_run_graph(create_initial_state(past_context+instrument_context))` → stream/invoke → 写 `~/.tradingagents/logs/<TICKER>/TradingAgentsStrategy_logs/full_states_log_<date>.json` → `memory_log.store_decision` → 清 checkpoint → `process_signal` 提 rating/action。CLI `run_analysis` 同逻辑但用 Rich Live（header/progress/messages/analysis/footer）+ `results/<ticker>/<date>/reports/*.md` 报告树。
- 决策记忆：`~/.tradingagents/memory/trading_memory.md` append-only markdown，pending→resolved（带 1 段 reflection），下次同 ticker 运行时注入 `recent same-ticker + cross-ticker lessons` 到 PortfolioManager prompt。
- 结构化输出：`bind_structured(llm, Schema)`，不支持的 provider 退化为自由文本再解析；`NO_EXTERNAL_TOOLS` 防止思考模型外发工具。

### 1.4 数据与 LLM 生态

- **LLM 12+ 统一工厂**：OpenAI(gpt-5.5/5.4) / Anthropic(Sonnet5/Haiku4.5/Fable5/Opus4.8) / Google(Gemini 3.5 Flash/3.1 Pro) / xAI Grok 4.x / DeepSeek V4 / Qwen 3.7 双域(DashScope intl/CN) / GLM 5 双域(Z.AI/BigModel) / MiniMax M3/M2 / Groq / Mistral / NVIDIA NIM / Kimi / Bedrock(langchain-aws) / Azure OpenAI / Ollama(REMOTE `OLLAMA_BASE_URL`) / OpenRouter / openai_compatible(vLLM/LM Studio/llama.cpp)。`model_catalog.py` 单真相源 + `capabilities.py` 成熟度表。`temperature=None` 时各 provider 默认；推理模型基本忽略 temperature。
- **数据 Vendors（显式路由）**：`core_stock_apis` yfinance/alpha_vantage · `technical_indicators` yfinance+stockstats(50/200 SMA,10 EMA,MACD,RSl,BOLL,ATR,VWMA, LLM 挑 ≤8) / alpha_vantage · `fundamental_data` yfinance/alpha_vantage · `news_data` yfinance/alpha_vantage · `macro_data` FRED(利率/通胀/就业/增长) · `prediction_markets` Polymarket(免 key) + Reddit(RSS 优先)+StockTwits+Yahoo News。`route_to_vendor(category, method)`：精确链，无静默回退；rate-limit→下一 vendor；`OPTIONAL_CATEGORIES` 降级为 `DATA_UNAVAILABLE` sentinel。
- **指标与回测**：stockstats 8 指标；无经典回测循环（backtrader 仅在依赖中，未接入 graph），取而代之“单日决策 + 5 日 realized/alpha + 反思”。

---

## 2. 亮点（Strengths）—— 值得 hero-quant 复用

### S1 高度模块化与显式契约（复用度 ★★★★★）

- **Graph/Propagator/ConditionalLogic/ToolNode 解耦**：`graph/setup.py` 中 analyst 可插拔 `selected_analysts tuple`，新增数据源只需在 `dataflows/interface.py:VENDOR_METHODS` 注册，零侵入。
- **Vendor 显式路由**（`dataflows/interface.py: route_to_vendor`）：`"default"` 尝试全部，否则精确链；对 `category+method` 强校验，`OPTIONAL_CATEGORIES` sentinel 降级而非静默吞错。对比 vibe-trading 的 fallback 链（5 次重试 + provenance），TradingAgents 更“可配置、可审计”。
- **配置单真相源**：`default_config.py:_ENV_OVERRIDES` 声明式 env→key 映射，`_coerce` 按默认值类型强转，拼写错误直接抛 `ValueError` 而非静默错配；`benchmark_map` 按后缀自动选区域指数（`600519.SS→000001.SS`、`0700.HK→^HSI`），解决 hero-quant 当前硬编码 SPY 的区域偏差。

**→ 落到 hero-quant：** 已有 `config/settings.py` 单 env 门 + `data/registry.py:16 源白名单`，补 `TRADINGAGENTS_*` 式的声明式覆盖表 + 区域 benchmark 自动映射 + 显式 vendor 链校验（当前 hero 偏隐式）。

### S2 幻觉治理的纵深防御（★★★★★）

- **身份锚定**：`agents/utils/agent_utils.py: resolve_instrument_identity` LRU256 + `build_instrument_context` 在每个 analyst prompt 前注入 “你正在分析的是 X 公司 (ticker Y, 交易所 Z)” ，根治 #814 类“分析错公司”幻觉。README 明确“不再变公司”。
- **价格校验**：`market_analyst` 强制 `get_verified_market_snapshot` + 8 指标去冗余表，任何价位声称必须能在快照中找到；`NO_EXTERNAL_TOOLS` 防止结构化输出时模型外发工具编造。
- **语言策略**：全链路 `output_language` 国际化，但 debate 固定英文保推理质量，`output_language` 仅影响最终报告，避免多语言污染推理。

**→ 落到 hero-quant：** 已有 `agent/grounding.py` 三级校验 + `Prompt 6 不变量`，可叠加：① `resolve_instrument_identity` 式“身份头”注入所有 tool 调用前；② `verified_market_snapshot` 式“价格证据块”作为 GroundingLedger 必检项；③ 结构化输出 `NO_EXTERNAL_TOOLS` 约束。

### S3 工程健壮性进化（★★★★☆）

- 近期大量 correctness fixes：Look-ahead 过滤、ticker 路径穿越防护（`_safe_sidecar_path` 同款思路）、stale OHLCV 拒绝、DFR retry/429 backoff（`llm_max_retries`）、图形状感知 checkpoint 签名、Windows UTF-8 修复、跨平台稳定。
- **Checkpoint  crash-safe**：`checkpointer.py` per-ticker SQLite + `thread_id=hash(ticker:date:signature)`，resume 时校验图形状，不匹配则废弃旧 checkpoint，避免“用错图”静默错。
- **可观测性**：CLI Rich Live 四栏（header/progress/messages/analysis/footer stats）+ `cli/stats_handler.py` LLM/tool/tokens/wall-time 统计 + 磁盘报告树 + memory log 双持久化。

**→ 落到 hero-quant：** 已有 `trace.py: tmp→fsync→link` + `ledger 0600+fsync` + `governance/dedup PG 三态`，可借鉴：① per-ticker/ per-run SQLite checkpoint（当前 hero `checkpoint/postgres.py` PG 可选但未 per-run 隔离）；② CLI Live 布局对 `api/server.py` SSE 流的进度可视化（当前 Chat.tsx SSE 仍偏 mock）。

### S4 最全 LLM 生态适配（★★★★☆）

- `llm_clients/factory.py + model_catalog.py + capabilities.py` 三件套：单一 model 目录、能力矩阵（是否支持 structured、tool_choice、thinking 字段）、provider 特有参数（`google_thinking_level / openai_reasoning_effort / anthropic_effort`）统一透传。
- 支持企业级：Bedrock API-key、Azure OpenAI、Ollama 远程 `OLLAMA_BASE_URL`、openai_compatible 任意端点（vLLM/LM Studio/llama.cpp）。`_CUSTOM_ONLY` 允许自定义 modelId。

**→ 落到 hero-quant：** 当前 `HERO_LLM_PROVIDER=openai|deepseek|anthropic` + `langchain-openai`，较窄。建议：① 引入 `model_catalog` + `capabilities` 表，支持 `llm_provider` 12+ 但保持 hero 的 `HERO_*` env 前缀；② 复用 `backend_url` 远程 Ollama / openai_compatible 能力，便于本地化部署；③ 区分 `deep_think` vs `quick_think` 双模型，成本分层（当前 hero 单模型）。

### S5 Prompt 工程扎实（★★★★☆）

- `market_analyst` prompt 强制“选 8 个不冗余指标 + 表格化 + 校验快照”，避免 LLM 堆砌指标。
- 结构化 Schema：`agents/schemas.py` 五级 `PortfolioRating` / 三级 `TraderAction` / `ResearchPlan/TraderProposal/PortfolioDecision/SentimentReport` + `render_*`，使 PortfolioManager 输出可解析、可审计。
- Vendor 提示词中“年度化风险仅基于 close_to_close” 等细节，降低误用。

**→ 落到 hero-quant：** `agent/prompt.py: build_system_prompt` 已有三级 grounding，可补：① 结构化 Pydantic 输出（当前 hero tool 返回多为自由文本）；② “指标去冗余 + 表格化” 约束进 `quantlib_tool`。

### S6 科研可复现性坦诚 + 反思闭环（★★★☆☆）

- README 单列 Reproducibility，坦诚：LLM 采样非确定 + 数据随时间漂移 + 推理模型忽略 temperature，给出降方差路径（`temperature=0.0` + 非推理模型 + Custom modelId）。
- 反思层：`graph/reflection.py` 在下次同 ticker 运行时取 5 日 realized/alpha → LLM 1 段反思 → 注入 PortfolioManager prompt，形成轻量“经验回放”。

**→ 落到 hero-quant：** 已有 `shadow_account` 5 类归因（missed/noise/early/late/overtrade），可叠加“5 日 alpha 反思”作为 `Research/Hypothesis` 5 状态的输入源之一。

---

## 3. 缺点（Weaknesses）—— 结构性风险

### W1 串行分析师是吞吐瓶颈（P0 严重）

- `build_analyst_execution_plan` 虽有 plan 抽象，实际为**顺序链**：`Market→Sentiment→News→Fundamentals` 必须串行；注释中“并行执行 planned”未实现。
- 1 次完整 propagate = 4 analyst × (LLM+tool) + `2*max_debate_rounds`（默认 1→2 轮） + `3*max_risk_discuss_rounds`（默认 1→3 轮） + ResearchManager/Trader/PortfolioManager ≈ **10-12 次 LLM 调用**。以 `gpt-5.5 pro $30/180 per 1M` 计，单 ticker 单日成本高、延迟分钟级，无法批量回测。

*证据：`graph/setup.py` / `graph/analyst_execution.py` / `default_config.py: max_debate_rounds=1`*

**对比：** vibe-trading 用 `ThreadPool(4)` 对 DAG 分层并行 + `_batch_execute` readonly 并行；hero-quant `loop.py: ThreadPoolExecutor(8) is_concurrency_safe` 已支持 readonly 并行，但 `use_graph` 的 `Send` fanout 仍为占位。TradingAgents 反而落后。

### W2 数据“未来泄露”悖论（P0 致命于回测）

- README 自承：**新闻/社交“反映现在”而非 trade_date**，即使有 lookback 窗口，历史回测仍泄露未来信息；FRED/Polymarket 为 optional，可降级为空，进一步削弱宏观一致性。
- 价格侧虽有 look-ahead 过滤，但**叙事侧无法固定历史快照**，导致“用 2026-08 的新闻去回测 2024-03”的伪回测。

*证据：`README.md: Reproducibility` / `dataflows/*` / `agents/analysts/sentiment_analyst.py` 三源合并*

**对比：** vibe-trading `backtest/loaders/registry.py` 按 market 统一 calendars + `searchsorted` ffill + 5(10) bar 限制 + `@register` provenance；hero-quant `backtest/engine.py: _align next-day open` + `validation.py: PIT weights_on≤price_date` + `engine.py: historical_base_price` 均为 PIT 正逻辑，显著更严。TradingAgents 在此维度不可用于严肃回测。

### W3 过度依赖 LLM 采样，不可复现（P0）

- 推理模型（默认 GPT-5.x / Gemini 思考模式）**忽略 temperature**，两次同参必分歧；结构化输出成熟度不均（DeepSeek/MiniMax 需 `capabilities` 跳过 `tool_choice`，否则空返回）。
- 无 `seed`/`fingerprint` 固化；无 VCR 录制；CI 仅防依赖缺失，无 golden-run。

*证据：`README Reproducibility` / `llm_clients/capabilities.py` / `tests/` 仅单测*

**对比：** dsh 的 `SessionEvent` 全量落盘 + `deriveMessages()` 可重放 + `sdk-runtime` 可录制；hero-quant 有 `ledger verify()` + `Trace 50k侧车` + `Test PIT正逻辑`，但同样缺 LLM VCR。需补。

### W4 Memory 设计局限（P1）

- 仅**同 ticker pending** 在下次同 ticker 运行时 resolve，跨 ticker pending 堆积；log 为**单文件 markdown** `TRADINGAGENTS_MEMORY_LOG_PATH`，无 DB 索引，随时间膨胀解析慢；`memory_log_max_entries=None` 默认不轮转。
- BM25 记忆已移除，新版仅“最近同 ticker + 跨 ticker lessons” 轻量注入，缺向量召回（hero-quant 曾有 80% context folding 向量方案，vibe 有 Ebbinghaus 14d 半衰期 + FTS5 三 gram）。

*证据：`agents/utils/memory.py: TradingMemoryLog` / `default_config.py: memory_log_*` / `graph/reflection.py`*

### W5 伪回测：缺真正引擎（P0）

- `pyproject.toml` 列 `backtrader`，但**graph 未接线**，`propagate` 仅单日决策，无 `Sharpe/drawdown/成本/滑点/资金` 仿真，无法批量评估；`results/` 仅报告树，不产 `positions.csv/fills.csv/metrics.json/tearsheet.html`。
- 论文中收益数字依赖外部后处理，非框架内闭环。

*证据：`pyproject.toml` / `graph/trading_graph.py` / `reporting.py`*

**对比：** hero-quant `backtest/engine.py` 事件驱动 `on_bar` + `turnover*cost 0.0005` + `equity cumprod` + `positions/fills/metrics/tearsheet monthly ME + drawdown episodes Top3` 完整；vibe 有 9 市场引擎 + 5 优化器。TradingAgents 在此差距最大。

### W6 Tool 粒度与幻觉残留（P1）

- `get_verified_market_snapshot` 仅 market analyst 强制，其余分析师仍可捏造；sentiment 三源合并但 StockTwits/Reddit 配额易枯竭时 sentinel 空洞，LLM 仍会“脑补情绪”。
- `DISCLAIMER` 免责声明 + `reproducibility` 坦诚，但**上游未做机械校验**（如 vibe 的 `GroundingLedger._validate_price_tables` 正则表校验）。

### W7 配置与部署复杂度（P1）

- `TRADINGAGENTS_*` 20+ env + CLI 8 步交互 + 12+ provider 各自 key/endpoint，新用户门槛高；checkpoint per-ticker SQLite 在**并发多 ticker 批量**时文件锁竞争；无 hash-lock 供应链（对比 vibe `requirements-lock.txt --require-hashes` / hero `requirements-lock.txt`）。
- Docker 仅单服务 `tradingagents` + 可选 `ollama`，无 Postgres/Temporal/Otel sidecar，生产级不足。

*证据：`default_config.py:_ENV_OVERRIDES` / `docker-compose.yml` / `cli/utils.py`*

### W8 测试金字塔失衡（P1）

- 50 tests 多为**路由/校验/schema 单测**，缺 E2E golden-run、LLM VCR、多市场 PIT 集成、大并发压测；`coverage.fail_under=0`。

---

## 4. 四方对比矩阵（同套路横评）

| 维度 | TradingAgents v0.3.1 | vibe-trading 0.1.13 (278k LOC) | deepseek-harness 0.1.0-rc.5 | hero-quant (30k 目标) |
|---|---|---|---|---|
| **定位** | 多智能体辩论研究脚手架 | 全能正确性量化工作台 | 通用 Everything-is-Plugin 底座 | 单智能体 NL→回测闭环内核 |
| **编排** | LangGraph StateGraph 顺序链 + debate/risk 条件路由 | 自建 ReAct Loop 1400L + 5 层 context + DAG Swarm(ThreadPool4) | Cordis 插件 + SessionEvent 全量录制 + turn/step waterfall | AgentLoop 10 控制点 + 可选 LangGraph plan→execute→verify(Send fanout 占位) |
| **并行** | 串行（4 analyst 串行） | DAG 分层并行 + readonly batch | 插件并行 + tool isConcurrencySafe | ThreadPool8 readonly 并行；graph Send 待补 |
| **LLM** | 12+ provider, model_catalog 单源, dual-region | 20+ provider, capabilities 矩阵, reasoning_content 保留 | pi-ai + LLM vocab(ctx.llm) | 3 provider, 单模型, 待扩 catalog |
| **数据** | 显式 vendor 链, 无 fallback, optional sentinel | 25 loaders, FALLBACK_CHAINS=5, provenance, 1% 阻断, cache v4 | fs/shell/subprocess 能力缝, 可换沙箱 | 16 源白名单, Tencent(CN board_lots×100) / Yahoo(US), 1% 阻断, synthetic 兜底 |
| **回测** | 无引擎，单日决策+5日 alpha 反思 | 9 市场引擎 + 5 优化器, T+1/±10/20/30%, 费用, 滑点, liquidation | 无（通用底座） | 事件驱动单引擎, PIT 正逻辑, 次日开盘, 比例缩放, tearsheet 月度 ME + Top3 drawdown |
| **量学** | stockstats 8 指标 | 460+ 因子, quantlib 249 funcs | 无 | sma/ema/rsi/bollinger/macd/mdd + Polars + BS |
| **治理** | checkpoint per-ticker SQLite + memory markdown | hash-lock, AST 沙箱, hash 链, ledger, SBOM | 12-级 typed events, Session 录制, hygiene 30+ 门 | ledger 0600+fsync, Trace tmp→link 50k, FTS5 trigram 80% 向量折叠, dedup PG 三态 |
| **幻觉治理** | instrument 锚定 + verified snapshot | GroundingLedger 2500L 机械校验 + correction loop 3 次 | tool guard + restrict | Grounding 3 级 + BudgetBreaker, 待补身份锚定 |
| **可观测** | Rich Live + stats_handler | 无 Prometheus/OTEL (已知 gap) | OTel 可插 | OTel 三档 + /metrics + heartbeat 4 层 + circuit 双桶 |
| **供应链** | 无 hash-lock | requirements-lock hash + pip-audit + gitleaks | pnpm 11.7 lock + vendored Cordis pin SHA | requirements-lock hash + Dockerfile --require-hashes |
| **成本** | 高(10+ LLM/次) | 中高(80+ tools) | 低(底座) | 低(15 精选工具, TopK5 向量路由目标) |
| **适合** | 论文/研报原型 | 生产全能 | 自建 Agent 框架 | SaaS 超越(多租户 RLS+计费+Live 熔断) |

**一句话差异：** TradingAgents 强在“角色叙事与 LLM 生态”，弱在“回测与并发”；vibe 强在“广度与正确性”，弱在“臃肿与可观测”；dsh 强在“可扩展与可录制”，弱在“领域语义”；hero-quant 已用 15% 功能实现 60% 内核机制，需以“**精、快、严**”错位超越。

---

## 5. 对 hero-quant 的可复用战术清单（按优先级）

### 5.1 立即复用（P0，0-2 周，零风险）

| # | 战术 | 来源 | 落点文件 | 验收 |
|---|---|---|---|---|
| T1 | **Instrument 身份锚定**：`resolve_instrument_identity(LRU)` + `build_instrument_context` 注入所有分析/工具 prompt 头 | TradingAgents `agents/utils/agent_utils.py:75` | `src/hero_quant/agent/context.py` `src/hero_quant/agent/grounding.py` | 同 ticker 误识别用例由 0→1 覆盖，`test_instrument_anchor` |
| T2 | **Verified Market Snapshot**：价格声称必须命中 OHLC 快照，否则 GroundingError | TradingAgents `agents/analysts/market_analyst.py` | `src/hero_quant/agent/grounding.py: assert_price` 扩展 `assert_snapshot` | 伪造价位 100% 拦截 |
| T3 | **区域 Benchmark 自动映射**：`benchmark_map` 后缀→指数，`benchmark_ticker` 覆盖 | TradingAgents `default_config.py:152-163` | `src/hero_quant/config/settings.py` `src/hero_quant/backtest/engine.py` | 600519.SS/0700.HK/7203.T 各自 alpha 正确 |
| T4 | **NO_EXTERNAL_TOOLS 约束**：结构化输出时禁工具，防模型走偏 | TradingAgents `agents/utils/structured.py` | `src/hero_quant/tools/registry.py` `structured` wrapper | DeepSeek/MiniMax 结构化空返回回归用例 |
| T5 | **声明式 Env 覆盖表**：`_ENV_OVERRIDES + _coerce` 强类型 | TradingAgents `default_config.py:10-68` | `src/hero_quant/config/settings.py` | 拼写 `treu` 启动即抛错而非静默 |

### 5.2 结构补强（P1，2-6 周，微内核演进）

| # | 战术 | 来源 | 落点 | 验收 |
|---|---|---|---|---|
| T6 | **Analyst 并行化**：Market/Sentiment/News/Fundamentals `Send` fanout 并行，`analyst_execution` 去串行 | TradingAgents 瓶颈反面 + vibe DAG + dsh isConcurrencySafe | `src/hero_quant/agent/graph.py:54` `loop.py: ThreadPool` | 4 analyst 并行后 wall-time ↓60%，`test_graph_fanout` |
| T7 | **显式 Vendor 链校验**：`VENDOR_METHODS` 注册表 + `route_to_vendor` 精确链，OPTIONAL sentinel | TradingAgents `dataflows/interface.py` | `src/hero_quant/data/registry.py:22` | 非法 vendor 启动报错，optional 降级不抛 |
| T8 | **Reflection 5日 Alpha 闭环**：realized/alpha→1 段 LLM 反思→StrategyStore/Decay | TradingAgents `graph/reflection.py` | `src/hero_quant/memory/store.py` `strategy_store/` | 同 ticker 二次运行注入 lessons，`test_reflection_injection` |
| T9 | **Model Catalog + 双模型分层**：`model_catalog` 单源 + `deep_think/quick_think` 成本分层 | TradingAgents `llm_clients/model_catalog.py` | `src/hero_quant/config/settings.py` `llm_clients/` | gpt-5.5 deep + gpt-5.4-mini quick 分流，cost ↓30% |
| T10 | **议会式轻辩论**：Bull/Bear 2 轮辩论精简为 `ResearchPlan` 的 `pros/cons` 结构化对抗（不引入 3 轮 risk 链） | TradingAgents 研究团队思想提炼 | `src/hero_quant/agent/graph.py` `verify` 节点 | 研报含 pros/cons + 置信度，`test_debate_pros_cons` |

### 5.3 战略超越（P2，1-3 月，SaaS 护城河）

- **T11 批量回测 Bench**：补 TradingAgents 缺的“批量评估”—— `TradingAgentsGraph` 单日决策只能点测，hero-quant 已有单引擎批量能力，需封装 `run_batch(tickers, dates)` → `metrics.json` 对比 Sharpe/turnover/MDD，并做 `benchmark_map` 区域化 alpha 归一。
- **T12 PIT 新闻快照**：解决 TradingAgents 未来泄露—— 引入 `news_article_limit` + `global_news_lookback_days` 但改为 **PIT 快照源**（如 `yfinance_news` 按 trade_date 过滤 + `polymarket` 历史归档），非 PIT 源标记 `non-PIT` 并在 tearsheet 披露。
- **T13 LLM VCR 录制**：借鉴 dsh `SessionEvent` 录制，hero 已有 `Trace 50k侧车`，增加 `llm_usage.json` + `full_states_log` 的 VCR 回放，使 `tests/` 可离线 golden-run（TradingAgents/vibe 均缺）。
- **T14 Checkpoint 生产级**：TradingAgents per-ticker SQLite 签名感知图形状，hero 需补 `AsyncPostgresSaver` DDL + Temporal 重放 + `tmp→link EEXIST` 原子性（已部分实现，需打通）。

---

## 6. 改进路线图（与 surpass-design 衔接）

> 已有 `docs/plans/2026-08-20-hero-quant-surpass-design.md` 定义 0-12 月 B 策略（30k LOC 微内核 + Trait + Rust 60 算子）。本节在其上叠加 TradingAgents 复用项。

### Wave A — 治理与正确性（本周，P0）

- A1 `T1+T2+T5`：身份锚定 + 校验快照 + Env 强转 → `config/settings.py` `agent/context.py` `agent/grounding.py`
- A2 `T3`：区域 benchmark → `backtest/engine.py` `tests/test_benchmark_map`
- A3 `T4`：结构化 NO_EXTERNAL_TOOLS → `tools/registry.py`

### Wave B — 并发与成本（2-4 周，P1）

- B1 `T6`：`graph.py` Send fanout 真并行（补 `loop.py` 8 线程池的 graph 侧）
- B2 `T7+T9`：显式 vendor 链 + model_catalog 双模型 → `data/registry.py` `config/settings.py`
- B3 `T8+T10`：5 日反思 + 轻辩论 → `memory/store.py` `agent/graph.py:verify`

### Wave C — 批量与可复现（1-3 月，P2）

- C1 `T11+T12`：批量 Bench + PIT 新闻快照披露
- C2 `T13`：LLM VCR + golden-run CI
- C3 `T14`：Checkpoint PG + Temporal 打通

**YAGNI 约束延续**（`surpass-design.md: 7`）：每新增 1 Loader/Skill 需证明 >20% 用户用或替换 1 个；不追工具数量（20 精选不动），不引入 TradingAgents 的 3 轮风险辩论链。

---

## 7. 风险与取舍

| 风险 | 应对 | 取舍 |
|---|---|---|
| 抄辩论导致成本飙升 | 仅做 `pros/cons` 轻量对抗，不复刻 `2*N + 3*N` 轮 | 研报深度↑ vs 成本可控 |
| Vendor 显式链降低容错 | optional 类别 sentinel + synthetic 兜底保留 | 可配置性↑ vs 容错↓（显式失败优于静默错） |
| 双模型增加配置复杂度 | catalog 单源 + env 覆盖表 + 默认 `gpt-4o-mini` | 成本↓30% vs 配置+1 |
| PIT 新闻源稀缺 | 标记 non-PIT 并披露，不伪装 PIT | 诚实性↑ vs 历史回测覆盖↓ |

---

## 8. 下一步（Phase 2）

1. **评审本设计**：确认 Wave A-C 优先级与 YAGNI 边界（尤其是否采纳 T10 轻辩论）。
2. **执行 `writing-plans` skill**：将 T1-T10 拆为 `docs/plans/2026-08-21-hero-quant-tradingagents-tasks.md` 的 2-5 分钟 TDD 任务（写测试→见红→最小实现→见绿→提交）。
3. **Subagent-Driven Build**：每任务 `sessions_spawn` implementer + spec-reviewer + quality-reviewer，TDD 强制。
4. **验收门**：`pytest -q` 全绿 + `pytest tests/test_pit -v` + `ledger verify()` + `Trace 50k` 不丢 + 批量 Bench 对比 vignette。

---

### 附：关键证据索引

- TradingAgents 架构：`tradingagents/graph/setup.py` `trading_graph.py:18` `agents/utils/agent_states.py` `agents/utils/memory.py:12`
- 亮点：`agents/utils/agent_utils.py: resolve_instrument_identity` `dataflows/interface.py: route_to_vendor` `default_config.py: benchmark_map` `llm_clients/factory.py` `cli/main.py: Rich Live`
- 缺点：`graph/analyst_execution.py` 串行链 `README.md: Reproducibility` `default_config.py: memory_log_max_entries=None` `pyproject.toml: backtrader` 未接线 `dataflows/interface.py: OPTIONAL_CATEGORIES`
- hero-quant 基线：`src/hero_quant/agent/loop.py:34` 10 控制点 `backtest/engine.py:18` PIT + 比例缩放 `backtest/metrics.py` `memory/store.py: FTS5` `telemetry/otel.py` `governance/ledger.py`

*本设计已提交 `docs/plans/2026-08-21-tradingagents-deep-dive.md`，作为 `2026-08-20-hero-quant-surpass-design.md` 的增量附录。*

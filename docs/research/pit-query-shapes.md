# 行情查询形状盘点（PR-122-b 分区依据）

- 代码基线：`a33c81a`（2026-11-02），范围 `src/`，不含 `tests/`。
- 方法：`grep -rn` 全仓找 `get_bars` / `get_market_data` / `get_bars_range` / `capture_snapshot` / `as_of` 的调用点；每条路径用本机 `.venv` 实跑默认参数（`HERO_DATA_MODE=synthetic`）取返回行数；PIT 用 `/root/hero-data/pit` 的 122-a 样本（1 标的、726 个日分区、2023-11-01~2026-10-30）。
- 口径边界：synthetic 按日历日生成，行数是上限（真实交易日更少）；耗时均为本机 5 次中位数。仓内没有生产查询日志，`traces/trace.jsonl` 是测试产物，**不能**当流量口径；真实网络耗时未测。

## 表1 取数路径清单

| # | 文件::函数 | 类别 | 形状 | 单次读取规模（默认/代表值，本机实测） | 依据 | 仓内调用方 |
|---|---|---|---|---|---|---|
| Q1 | `tools/market_data.py::get_market_data` → `MarketDataRegistry.get_bars` | 查询 | **时序**：1 标的 × 日期区间 | 3 行（默认 2026-08-01~08-03，返回 3 条） | 实跑默认参数 `len(bars)=3`；每次 1 次 `get_bars` | Agent 工具调度、`api/server.py::_load_backtest_prices` |
| Q2 | `tools/market_data.py::get_bars_range` | 查询 | **批量（逐标的）**：N 标的 × 共享区间 | 3 标的 × 3 天 = 9 行，3 次 `get_bars` | 实跑 3 标的默认参数，合计 9 行 | Agent 工具调度、MCP |
| Q3 | `tools/quantlib_tool.py::compute_indicator` → `_fetch_closes` | 查询 | **时序**：1 标的 × 指标窗口 | 20 行（默认 08-01~08-20，window=20；返回 20 个值） | 实跑 `len(values)=20`；1 次 `get_bars` | Agent 工具调度 |
| Q4 | `tools/quantlib_tool.py::compute_factor` → `_fetch_closes` | 查询 | **时序**：1 标的 × 因子窗口 | 20 行；默认参数下 0 个值（bars<=window，fail-closed） | 实跑读 20 行、返回 `ok=False`；1 次 `get_bars` | Agent 工具调度 |
| Q5 | `tools/correlation.py::compute_correlation` | 查询 | **时序（双标的对齐）**：2 标的 × 区间 | 2 标的 × 32 天 = 64 行；实跑发起 **4 次** `get_bars`（2 次取 prov + 2 次取 closes），物理读取 128 行 | 实跑 `points=31`、计数器 `get_bars=4` | Agent 工具调度 |
| Q6 | `tools/backtest.py::run_backtest` → `_fetch_bars_for_backtest` | 查询 | **时序**：1 标的 × 用户区间 | 3 行（默认 08-01~08-03；用户可传数年） | 实跑默认参数 `len(bars)=3`；1 次 `get_bars` | Agent 工具调度 |
| Q7 | `api/server.py::_load_backtest_prices` → `get_market_data` | 查询 | **时序**：1 标的 × 回测窗口 | 60 行入引擎（日历回看 130 天，`tail(60)`） | 代码常量 `_BACKTEST_BARS_TARGET=60` + `tail` | `/v1/backtest` bundle 四个端点 |
| Q8 | `data/pit_store.py::PitParquetStore.as_of` | 查询（库 API） | **时序**：1 标的 × [start,end] | 1 天 1 行 / 20 天 17 行 / 3 年 726 行 | 实跑：1 天 99.3ms、20 天 131.0ms、3 年 600.7ms（5 次中位数） | 仓内无调用方 |
| Q9 | `data/snapshot.py::SnapshotStore.as_of` | 查询（库 API） | **时序**：1 标的 × [start,end]（PG） | 同 Q8 行形状，上限 `limit` | SQL 以 `symbol =` + `trade_date` 范围过滤 | 仓内无调用方 |
| Q10 | `data/snapshot.py::capture_snapshot` | 摄入 | **批量（逐标的）**：N 标的 × 窗口 | 3 标的 × 1 年 = 1095 行（逐标的循环） | 用 registry 按同循环实跑；未跑 PG 写入 | `scripts/snapshot_sample.py` |

**非取数入口（单列，不混入比例）**

| 文件::函数 | 实际读取 | 原因 |
|---|---|---|
| `tools/market_data.py::get_ticker_info` / `get_fundamentals` / `search_symbol(s)` | 0 行 | 本仓无数据源，固定返回 `no_data` |
| `tools/quantlib_tool.py::screen_factors` | 0 行 | `not_implemented` 占位，未读行情 |
| `tools/quantlib_tool.py::compute_sharpe` / `compute_drawdown` | 0 行 | 只吃入参 `prices` 列表 |
| `tools/backtest.py::optimize_portfolio` | 0 行 | 等权占位 |
| `data/quality.py::check_bars` | 0 行 | 纯函数，吃传入 rows |
| `api/risk.py::_get_cross_source` | 0 行 | 只做方法探测，不取行情 |
| `backtest/engine.py::BacktestEngine.run` | 0 行 | 消费调用方传入的 DataFrame |
| `data/sources.py` / `data/loaders/*` | — | 白名单/被 registry 调度，无独立调用点 |

## 表2 比例统计

| 口径 | 时序型 | 批量（N 标的逐次取） | 真横截面（一次查询取一批标的） | 样本与算法 |
|---|---|---|---|---|
| **数量占比** | **8 / 10 = 80%** | 2 / 10 = 20% | **0 / 10 = 0%** | Q1-Q10；仅计有仓内调用方的 8 条则为 6/8=75%、2/8=25% |
| **数据量权重** | **1686 行 = 99.5%** | 9 行 = 0.5% | 0 | 每条查询路径各跑一次代表性调用后求和（Q1-Q9；Q10 摄入不计）；剔除 Q8/Q9 两条 3 年长窗后仍为 234:9 = 96.3%:3.7% |

补充：Q10 摄入单次 1095 行，若并入同一口径，批量份额升至 39.6%（1104/2790）。但它是回填写入的前置读取，且实现仍是逐标的循环，不改变一级分区键的选择。

## 表3 分区粒度实测（同一批 726 行，SNAPPY→SNAPPY，隔离压缩变量）

| 布局 | 文件数 | 总字节 | 1 天查询 | 20 天查询 | 3 年查询 |
|---|---|---|---|---|---|
| 现状 `tenant=/symbol=/trade_date=日` | 726 | 1,929,708 | 99.3 ms | 131.0 ms | 600.7 ms |
| 重分区 `tenant=/symbol=/year=` | 4 | 31,108 | 4.6 ms | 4.9 ms | 6.7 ms |

- 现状每个文件恰好 1 行、2658 B，footer 中位数 1836 B，占文件 69.1%（pyarrow 元数据实测，前 50 个文件）。
- 重分区实验在 `/tmp` 完成并已删除；未改 `src/`、未改 PIT 样本。
- 当前 `PitParquetStore.as_of` 的 glob 是 `trade_date=*/part-000.parquet`，**start/end 不参与文件裁剪**；所以 1 天查询也要打开全部 726 个文件（99.3 ms）。

## 结论

**122-b 应优先按「时序型」分区：保留 `tenant/symbol` 一级前缀，把时间二级分区从「日」粗化到「年」（必要时「月」）；不要改成 `trade_date` 一级分区。**

依据：
1. 10 条取数路径中 8 条（80%）是「单标的 + 日期区间」；另 2 条批量路径也都按标的循环，仓内没有任何「一次查询取一批标的」的真横截面读取。
2. 所有真实读取的第一过滤条件都是 `symbol`，日期只做范围过滤；按 symbol 一级分区可把 20 天窗口从 726 个文件降到 1 个。
3. 实测 `symbol/year`：文件 726→4、体积 1.93 MB→31 KB、20 天查询 131→4.9 ms、3 年 600.7→6.7 ms。
4. 横截面需求目前只出现在评测集（`screen_factors` 是 stub、`get_fundamentals` 无数据源），不足以当一级分区键；将来真出现时用 date-first 物化视图或二级索引，而不是改主布局。

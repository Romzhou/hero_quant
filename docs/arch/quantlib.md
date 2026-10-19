# QuantLib + Backtest 模块深挖（自研指标库 + PIT 回测）

> 依据：`docs/recon.md` §1/§3（链B）、`docs/issues.md` 附链路行号。行号为 2026-10-19 快照实测值；链B路由行号相对 recon 有整体下移（漂移，详见 §2）。

## 1. 职责一句话

`src/hero_quant/quantlib/` 是自研纯函数指标库（SMA/EMA/RSI/Bollinger/MACD/MaxDD + 期权 BS/IV，pandas 主路径 + polars/rust 加速变体）；`src/hero_quant/backtest/` 是回测引擎（`BacktestEngine.run` 确定性计算 + `bench.run_batch` 合成路径诚实化）；链B经 `server.py:_get_backtest_bundle` 以 L1 内存 → L2 Redis → 重算 → 静态兜底四级提供产物。

## 2. 关键文件与行号

| 文件 | 行 | 内容 |
|---|---|---|
| `src/hero_quant/quantlib/indicators.py:63,82,98,143,185,220` | `sma/ema/rsi/bollinger/macd/max_drawdown` | pandas 主路径；参数校验 `_validate_window(:47)`、级联 `_to_series(:17)` |
| `src/hero_quant/quantlib/polars_base.py:64,114,121` | `sma_polars/ema_polars/rsi_polars` | polars 变体；缺 polars 时 `_require_polars(:21)` 抛错 |
| `src/hero_quant/quantlib/rust.py:31,47,87,123,151,179,206,230` | `is_rust_available/_use_rust` + 同名指标 | Rust（`pyo3 0.21` stub，见 recon §1）可用才走加速，否则回退 Python |
| `src/hero_quant/quantlib/options.py:58,129,214` | `bs_price/bs_greeks/implied_volatility` | BS 定价/希腊字母/隐波；`_resolve_bs_args(:30)` 归一化别名参数 |
| `src/hero_quant/backtest/engine.py:165` | `class BacktestEngine:` | 回测引擎 |
| `src/hero_quant/backtest/engine.py:514` | `def run(` | 确定性回测主入口（输入 prices + weights → metrics/positions/tearsheet） |
| `src/hero_quant/backtest/engine.py:1119-1129` | tearsheet 渲染 | `sharpe/max_drawdown/annual/vol/cum/turnover` 格式化行 |
| `src/hero_quant/backtest/bench.py:6` | 合成诚实化 | `run_batch` 为 synthetic-only，所有 metrics 带 `non_pit=True` |
| `src/hero_quant/backtest/bench.py:27-57` | tearsheet 披露 | 强制含 `non-PIT source/unavailable` 字面（`:50,57` 双保险） |
| `src/hero_quant/backtest/bench.py:69-104` | `_build_pit_disclosure` | 无 pit 字段/部分未验证/委托缺标记三种情况一律补 `non-PIT` 后缀（`:92-93` “不直接透传伪装 PIT”） |
| `src/hero_quant/tools/backtest.py:123` | `def run_backtest(` | Agent 可调工具封装 |
| `src/hero_quant/api/server.py:2111` | `def _get_backtest_bundle():` | 链B bundle：L1 内存 → L2 Redis → 计算（recon 写 `:1721`，实测 **漂移+390 行**） |
| `src/hero_quant/api/server.py:2060` | `def _compute_backtest_bundle` | 重计算：20 日确定性合成行情（`:2066-2074`，类 600519.SH 走势，无随机性）+ 缺省 `sharpe=1.62/annual_return=0.184`（`:2077-2080`）+ CSV/tearsheet 兜底（`:2084-2097`） |
| `src/hero_quant/api/server.py:2042` | `def _write_backtest_bundle_cache` | L2 写（JSON `ex=600`，DataFrame 只存 csv/tearsheet/metrics，见 `:2043` docstring） |
| `src/hero_quant/api/server.py:2179,2195,2212,2315` | 四产物路由 | `metrics.json/positions.csv/tearsheet.html/drawdowns.json`（recon 写 `1789/1805/1822/1925`，实测 **整体下移约+390 行**） |
| `src/hero_quant/api/server.py:2338` | `GET /v1/trace/events` | 按 `offset` 切片读 `TRACE_PATH`（recon 写 `:1948`，实测 **漂移**） |
| `src/hero_quant/api/server.py:1926` | bundle 消费点 | 流内 `_bundle = _get_backtest_bundle()` |
| `src/hero_quant/infra/redis.py:213,281` | `get_redis_sync/get_redis` | L2 Redis 同步/异步入口；键前缀 `hero:cache:`（`:27`）、`hero:ratelimit:`（`:26`） |

## 3. 上游 / 下游

- 上游：前端 tearsheet 页 + Agent `run_backtest` 工具（`tools/backtest.py:123`）→ 统一走 `_get_backtest_bundle(:2111)`。
- 下游：`BacktestEngine.run(:514)` 纯计算；`bench.run_batch` 只用于合成/基准场景且强制 `non_pit` 标记；L2 经 `infra/redis`。

## 4. 四级产物语义（L1 → L2 → 重算 → 静态）

1. L1：`_backtest_cache` 内存（double-checked locking，`:2117-2126` docstring 见 `:2112-2116`）。
2. L2：Redis JSON（`_read_backtest_bundle_cache`，`ex=600`），读失败 `logger.debug`（`:2038`，P2-1 候选升级点）。
3. 重算：`_compute_backtest_bundle(:2060)`，分布式锁拿不到则 fail-open 继续算（`:2127` “拿不到则 fail-open 继续算；fakeredis 下兼容”）。
4. 静态兜底：CSV 默认行 + tearsheet 默认 HTML（含 `Sharpe 1.62 | Annual 18.4%`，`:2097`）——**演示数语义**，前端必须按 degraded/演示标识处理（挂钩 P0-3/P1-1 类风险，链B侧）。

## 5. 降级语义

| 场景 | 语义 | 依据 |
|---|---|---|
| Redis 不可达 | fail-open：跳过 L2 直接重算（dlock 失败 `logger.debug` + 继续，`:2143-2147`） | `server.py:2127-2147` |
| 重算异常 | 静态兜底 CSV/tearsheet（`:2084-2097`），但必须让消费者知道这是兜底（非真实回测） | `server.py:2060-2098` |
| bench/合成路径 | 一律 `non-PIT` 披露，metrics 带 `non_pit=True`；禁止把合成 tearsheet 当实盘证据 | `bench.py:6,50,57,69-104` |
| Rust/polars 缺失 | 回退纯 Python（`_use_rust(:47)` / `_require_polars(:21)`），不阻断 | `quantlib/rust.py:47`；`polars_base.py:21` |

## 6. 已知问题挂钩

- P0-3 前端零构建假活：链B tearsheet 静态兜底（`:2097` 假 HTML 恒 `Sharpe 1.62`）与 SPA 占位（`:2452-2464`）同类风险——无 `dist`/无真实计算时必须 503 或 degraded 声明，不得恒 200 假页面。归 D（语义）+ E（消费）。
- P2-1：`server.py:2038,2057,2143,2147` 四处链B `logger.debug + 继续` 为“静默降级”候选，需按链B分级提 `warning` + 脱敏字段（D 线）。

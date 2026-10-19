# 性能基准 (L线)

> 生成脚本：`scripts/bench_quantlib.py`（合成数据 deterministic，seed 固定，不触网、不读 `.env`）。
> 可复现命令（仓库根）：
>
> ```powershell
> pip install -e . --no-deps   # e2e 同一 python 环境装后端（requirements-lock 不动）
> python scripts/bench_quantlib.py --repeat 5
> python scripts/bench_quantlib.py --repeat 5 --json docs/bench_result.json
> npx playwright test           # 4 project 全跑对照
> ```

## 机器配置

| 项 | 值 |
|---|---|
| CPU | AMD Ryzen 5 3500U with Radeon Vega Mobile Gfx |
| OS | Windows 11 (10.0.22631, `Windows-11-10.0.22631-SP0`) |
| Python | 3.12.10 (`C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe`) |
| pandas / numpy | 3.0.6 / 2.5.3 |
| hero_quant | editable 安装（`pip install -e . --no-deps`，无版本/哈希变更） |
| Playwright | 1.62.1 |

## 结果 (2026-10-19, repeat=5, n_small=1000, n_big=5000)

| case | mean_ms | min_ms | max_ms | rows/s |
|---|---:|---:|---:|---:|
| sma(n=1000) | 0.738 | 0.658 | 0.823 | 1355014 |
| ema(n=1000) | 0.675 | 0.597 | 0.861 | 1481481 |
| rsi(n=1000) | 9.658 | 6.868 | 12.732 | 103541 |
| bollinger(n=1000) | 3.218 | 2.603 | 5.611 | 310752 |
| macd(n=1000) | 3.220 | 2.373 | 4.288 | 310559 |
| max_drawdown(n=1000) | 2.537 | 1.409 | 3.625 | 394166 |
| sma(n=5000) | 1.333 | 1.088 | 1.632 | 3750938 |
| rsi(n=5000) | 9.929 | 7.832 | 11.715 | 503575 |
| macd(n=5000) | 3.217 | 2.293 | 4.304 | 1554243 |
| engine.run(252d) | 1034.337 | 874.491 | 1201.363 | - |
| engine.run(1000d) | 4396.706 | 3699.597 | 4950.188 | - |
| bench.run_batch(3 tickers x 5d) | 301.538 | 238.223 | 380.726 | - |

说明：

- 指标 case 均为纯 pandas/numpy（`src/hero_quant/quantlib/indicators.py`），rsi 含两路 Wilder EWM 故最慢（~10ms/1k 行），属预期。
- `engine.run` 为回测核心路径（`BacktestEngine.run(prices, allow_synthetic=True)`，含指标/权益/tearsheet 产出），252 日 ~1.0s、1000 日 ~4.4s，随数据长度超线性增长——tearsheet/事件循环是主要成本，后续优化点（非本线范围）。
- `bench.run_batch(3 tickers x 5d)` 跑 6 次 engine（含策略+基准腿）约 300ms，因 5 日小数据量引擎固定开销占主导。
- engine/bench 均为 synthetic-only（`allow_synthetic=True`），结果禁用于实盘阈值（与 `bench.py` 诚实化标记一致）。
- 波动：同机 repeat=5 下 engine.run max/min 差约 1.3x，属 Windows 单机正常抖动；指标 case 稳定（<2x）。

## e2e 对照（同环境同日）

`npx playwright test`：10 passed（11.8s），4 project 全绿，见终态结果表。
环境修法即本页顶部 `pip install -e . --no-deps`（`requirements-lock.txt` 未动，不放宽哈希锁）。

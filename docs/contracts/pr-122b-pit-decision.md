# PR-122-b PIT 分区决策与前置（2026-11-02）

> 决策来源：批次16 阶段 C 的两份实测（`docs/research/pit-query-shapes.md`、`docs/research/pit-partition-benchmark.md`）。
> 状态：**已定，未开工**。两个前置按顺序完成前不许动 122-b。

## 1. 决策：分区键 = year_month

- 布局：`tenant=<t>/symbol=<s>/year_month=<YYYY-MM>/part-000.parquet` 与 `tenant=<t>/year_month=<YYYY-MM>/symbol=<s>/...` 二者择一在 122-b 设计时按查询实测定；一级键是 **year_month**。
- 依据（实测，300 标的 × 3 年 / 201,973 行）：
  | 方案 | 文件数 | 体积 | 时序冷/热 | 横截面冷/热 |
  |---|---|---|---|---|
  | trade_date（现状） | 726 | 14.60 MB | 399/369 ms | 69/71 ms |
  | symbol | 284 | 4.87 MB | 25/27 ms | 148/98 ms |
  | **year_month** | **36** | **4.94 MB** | **36/27 ms** | **31/20 ms** |
- 取舍：year_month 在两类查询上都接近最优（时序比 symbol 多 11ms，横截面快 5 倍）；文件数随时间线性（约 12/年），不随标的数膨胀。
- 保留 C1 的洞见：文件内按 `(symbol, date)` 排序，并提供 symbol→row_group 索引，时序查询仍可裁剪（122-b 验证项）。

## 2. 前置一：修 `PitParquetStore.as_of` 的文件裁剪

- 现状：glob 只拼 `trade_date=*/part-000.parquet`，`start/end` 不参与文件选择 → 查 1 天也要打开全部 726 个文件（实测 99.3ms）。
- 要求：按 `start/end` 生成候选分区集合（year_month 前缀），只读命中的分区；保持只读快照隔离与 as_of 语义不变。
- 验收：修完后**重跑 C2 的三方案耗时对比**并替换基准表；1 天查询打开文件数必须 < 10（对比现状 726）。

## 3. 前置二：instrument master（上市/退市/停牌）

- 现状：腾讯 qfqday 只有 `date/open/close/high/low/volume` 六列，**没有任何状态字段**；300 只抽样里 16 只返回 0 行（退市/ST 侧证）。
- 要求：独立建 instrument master（至少 `symbol, listed_at, delisted_at, status, halt_flags`），回测按 as_of 过滤"当时可交易"。
- 不做这一条的后果：PIT 只是一张复权价格表，**防不了幸存者偏差**。

## 4. 硬性规则

1. **0 行标的必须显式标记不可用**（`status=no_data` 或 `delisted`），不许静默当"无数据"跳过。
2. 入库锁 `qfq` 并记录 `factor_asof`；禁止与不复权数据混用（实测口径差中位 1.54%、最大 55.67%）。
3. 质量门按"窗口起点已上市"的 cohort 判定（实测 p90 0.69% / p99 2.20%），新股从首根 K 线起算。
4. 双源对账只有东财可用（Yahoo 全端点 403）；恢复第二源前，交叉校验一律标 `single_source`，不假装做了双源。

## 5. 未做

- 未改 `src/`、未建表、未写 122-b 代码。
- 未测并发与更大规模（>300 标的 / >3 年 / 分钟线）。
- 未审计除权因子与公司行为。

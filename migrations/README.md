# migrations/ — 已废弃（DEPRECATED）

本目录的 SQL 文件**不再生效**，仅作历史参考，请勿在此修改 DDL。

## 实际建表 DDL 由 Python 代码内联定义并在运行时执行

- **checkpoint**：`src/hero_quant/checkpoint/postgres.py` 的 `DDL_CHECKPOINTS`
- **billing**：`src/hero_quant/billing/service.py` 的
  `DDL_FACTORS` / `DDL_PURCHASES` / `_BILLING_RLS_STATEMENTS` / `_BILLING_INDEX_STATEMENTS`

## 为什么废弃

这两个 SQL 文件与 Python 内联 DDL 早已 drift（例如 Python 侧有 `run_text`、
`idempotency_key`、`UNIQUE(factor_id, buyer_tenant)` 列，本目录的 SQL 没有），
且没有任何代码或部署配置（`Dockerfile` / `docker-compose.yml`）引用本目录。

## 修改 schema 的正确方式

改上述 Python 内联 DDL 字符串，不要改本目录的 `.sql`。

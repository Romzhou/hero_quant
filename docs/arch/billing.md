# Billing 模块深挖（fail-closed 计费 + RLS）

> 依据：`docs/recon.md` §3（存储/billing）、`docs/issues.md` P0-4/P1-4、`migrations/002_billing_rls.sql`。行号为 2026-10-19 快照实测值。

## 1. 职责一句话

`src/hero_quant/billing/` 提供因子发布/售卖的强一致记账：PG 真实池存在时走 `UNIQUE + ON CONFLICT` 权威语义 + RLS 租户隔离；无真池时一律 fail-closed（返 `False/None` 并由调用方抛错），绝不伪装持久化成功。

## 2. 关键文件与行号

| 文件 | 行 | 内容 |
|---|---|---|
| `src/hero_quant/billing/service.py:169-187` | DSN 判据 | `_is_pg_dsn(:169)`、`_dsn_key(:173)`、`_purchase_guard(:187)`（防重键） |
| `src/hero_quant/billing/service.py:215-228` | DSN 来源 | `_billing_dsn_from_env`：`HERO_BILLING_DSN → HERO_PG_DSN → HERO_CHECKPOINT_DSN` 顺序（`:218`）；`BillingService.__init__`（`:228`） |
| `src/hero_quant/billing/service.py:225` | 服务类 | `class BillingService:` |
| `src/hero_quant/billing/service.py:252,261` | 模式判据 | `_is_pg_mode(:252)`、`_is_real_pg(:261)`（仅真实池才算 PG） |
| `src/hero_quant/billing/service.py:283-286` | 真实发布 | `_real_pg_publish_sync`；docstring “无真实池时返 False（fail-closed，不伪成功）；commit 异常记 billing commit failed 并返 False” |
| `src/hero_quant/billing/service.py:300-312` | 废弃桩 | `_pg_publish_noop(:300)` / `_pg_purchase_noop(:309)` “一律 fail-closed 返 False”（兼容别名） |
| `src/hero_quant/billing/service.py:317-318` | DDL | `_exec_billing_ddl` “真实 PG 上幂等建表 + 索引 + RLS，失败抛错由调用方 fail-closed” |
| `src/hero_quant/billing/service.py:326-444` | 发布主路径 | `publish_factor`；`:373-375` “无真 pool 时 fail-closed 拒绝售卖/发布；(factor,buyer) 唯一权威走 PG UNIQUE+ON CONFLICT”；`:385` “PG 持久化优先，成功后再落内存与 ledger；False 需 fail-closed，不写内存不追加 ledger”；`:444` `raise RuntimeError(f"PG publish failed (emulated fail-closed) for factor_id=...")` |
| `src/hero_quant/billing/service.py:496-574` | 发布同步体 | `_real_pg_publish_sync(:496)` / `_pg_publish_sync(:569)` “无真实池 fail-closed 返 False（PG persistence not implemented 告警，不伪成功）”（`:573-574`） |
| `src/hero_quant/billing/service.py:631,641` | ledger 补偿 | `_enqueue_pending_ledger(:631)` / `replay_pending_ledger(:641)`（pending 重放） |
| `src/hero_quant/billing/service.py:663,689,715` | 读/买 | `list_factors(:663)` / `get_factor(:689)` / `purchase(:715)`；`:729` “无真 pool 的 PG DSN 路径 fail-closed 拒绝售卖” |
| `src/hero_quant/billing/service.py:969-1053` | 购买同步体 | `_pg_insert_purchase_sync(:969)`（`_run(conn):991`）/ `_pg_purchase_sync(:1043)` “一律 fail-closed 返 False” / `_pg_delete_purchase_sync(:1053)` |
| `src/hero_quant/billing/service.py:1093-1094` | 废弃查询桩 | `_pg_get_factor_sync` “一律 fail-closed 返 None，勿作成功判据” |
| `src/hero_quant/config/settings.py:204-228` | 网关 | `billing_dsn` “仅显式 HERO_BILLING_DSN 生效；默认 fail-closed 返 None”（`:204`）；空 DSN 回退 `HERO_PG_DSN/HERO_CHECKPOINT_DSN` 时 `warnings.warn + logger.warning`（`:224-228` “billing and checkpoint will share DB”） |
| `src/hero_quant/api/server.py:953-974` | `/ready` 计费探针 | `_check_billing_pg_full`：空 DSN → `reason="billing DSN 缺配 fail-closed(memory)"`（`:965`）+ `logger.warning(error_code="billing_dsn_missing", dsn_host="***")`（`:966`，脱敏范例） |
| `migrations/002_billing_rls.sql:10,20` | 表 | `factors(:10)`、`purchases(:20)`；`:17` 复合唯一供复合 FK；`:27-28` 复合 FK 防跨租户 orphan |
| `migrations/002_billing_rls.sql:31-38` | 索引 + RLS | `idx_factors_tenant/idx_purchases_tenant/idx_purchases_buyer/idx_purchases_factor(:31-34)`；`ENABLE + FORCE ROW LEVEL SECURITY(:37-38)` |
| `migrations/001_checkpoint.sql:5` | checkpoint 表 | `CREATE TABLE checkpoints`（billing  twin 表，DSN 共享告警见 settings `:224-228`） |

## 3. 上游 / 下游

- 上游：`/v1/*` 售卖路径 + `/ready` 就绪探针（`:953-974` 全量探针，reason 透出）。
- 下游：真实 PG（`factors/purchases` + RLS）→ 内存/全局（仅 PG 成功后才落，`:385`）→ ledger（追加失败回滚内存/全局并抛错，`:383`）→ pending 补偿（`:631-641`）。

## 4. 降级语义（全 fail-closed，与 memory 向量回退相反）

| 场景 | 语义 | 依据 |
|---|---|---|
| `HERO_BILLING_DSN` 为空 | fail-closed：默认返 `None`，计费路径拒绝；`/ready` reason 明示 `billing DSN 缺配 fail-closed(memory)` | `settings.py:204-214`；`server.py:963-966` |
| 无真实池（emulated/内存） | 发布/购买返 `False` → 调用方抛 `RuntimeError`（`:444`），不写内存、不追加 ledger | `service.py:373-444,573-574,729` |
| commit 异常 | 记 `billing commit failed` 并返 `False`（不伪成功） | `service.py:286` |
| ledger 追加失败 | 回滚内存/全局写入并抛错 | `service.py:383` |
| RLS 拒绝 | 透到 `/ready`（D 线要求），禁止 `except pass` 吞错 | recon §6 Fail-Closed；`server.py:953-974` |
| 废弃桩被旧 monkeypatch 调用 | 仍 fail-closed（`_pg_publish_noop/_pg_purchase_noop/_pg_get_factor_sync`） | `service.py:300-312,1043-1094` |

## 5. 已知问题挂钩

- P0-4 缺配 `raise`（`checkpoint/postgres.py:284-286`）：billing 侧对应语义已在 `service.py:444` 落地 `RuntimeError`；两者都要在 `/ready` reasons 如实暴露（D 线）。
- P1-4 `settings.py:172` + `server.py:214` 双写死 `localhost` 与 compose `postgres` 矛盾：billing DSN 回退链（`settings.py:220-228`）受此影响——compose 内若只配了 `HERO_CHECKPOINT_DSN=postgresql://localhost...` 则连失败后 billing 跟着 fail-closed。归 A 线（`settings.py` 统一切换）。

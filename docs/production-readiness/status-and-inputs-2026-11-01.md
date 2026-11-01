# hero-quant 进度与待你决策的输入（2026-11-01）

> 基线：master `22b78b0`（已 push，GitHub Actions `test`/`CI` 绿）
> 门禁实跑：`ruff check src` 全过；pytest **3331 passed / 61 skipped**；PG 族 **33 passed**；前端 vitest **117 passed**；e2e **32 passed / 6 project**；`npm run build` 通过；pip-audit 无已知漏洞；gitleaks 无泄漏。
> 进度真源：`progress-2026-10-29.md` §1~§18；任务总表：`backlog.md`（67 项）。

---

## 1. 一句话现状

Phase 1 只剩 **PR-122**（PIT 全量数据层）一项；Phase 2 刚起步（PR-214 已完成）；Phase 3 未开始。
真正卡住进度的不是代码量，而是 **4 个需要你拍板的输入**：依赖审批（duckdb/pyarrow、temporalio）、磁盘、生产机、Vault/KMS。

## 2. 阶段完成度

| 期 | 进度 | 说明 |
|---|---|---|
| Phase 1 | **35/36** | 仅剩 PR-122；其余身份/模型/数据/会话/观测/安全/部署/合规/发布均已落地（部署类为"脚本就绪、生产机未验"） |
| Phase 2 | **1/17** | PR-214 已完成；余下 Billing/调度/审批/渗透/RAG/审计不可篡改/Prompt 版本等 |
| Phase 3 | **0/14** | 备份 DR/压测/成本配额/隐私删除/运营后台，全部依赖生产机或 Phase 2 |

## 3. 本会话已完成（批次 10~13，均 ff 合入并 push）

| 批次 | 内容 | commit |
|---|---|---|
| 10 | **PR-214** Risk 归因/Shadow 真数据：5 类归因由真实 positions/prices 实算（恒等式 1e-9），新增 `/v1/risk/attribution`、`/v1/risk/shadow`，前端删除全部写死数字 | `18e39fc` |
| 10 | **PR-151** 密钥托管抽象：provider 契约 + env 后端 + 轮换三态记账；vault/kms fail-closed | `0622e0a` |
| 11 | **PR-151 接线**：LLM factory / gateway / embed / rerank 的 key 全走 `SecretsProvider` | `da21087` |
| 12 | **PR-151 续**：PII 加密口令接线 + `pii_key_status()` 探针（取不到即拒绝加解密） | `be5a636` |
| 13 | **PR-151 续**：PG 口令在「DSN 有用户名无口令」时从 provider 注入（百分号编码、query 保留） | `e54dcf0` |
| 修复 | certifi 安全公告、CI gitleaks 浅克隆假红、j14 断言冲突、Risk 页告警展示、Risk 用例抢跑 | `bb59028` `c06e7a9` `8a9390c` `cc43151` `22b78b0` |

## 4. "做一半"清单（有实测证据，不是估计）

| 项 | 现状 | 差在哪 |
|---|---|---|
| PR-124 可复现 | 已有 `config_hash / data_fingerprint / result_hash` | 指纹绑的是"本次行情"，不是 PIT `snapshot_id`；要等 PR-122 |
| PR-112 真实评测 | 36/50（0.72） | 12 条 latency 超预算（模型速度，非代码抖动）；usage 全 `estimated`、cost 为 null（网关不回 usage） |
| PR-151 密钥 | 抽象 + env 后端 + LLM/PII/PG 接线 | 无 Vault/KMS；无自动轮换/吊销；轮换未接审计；Redis 口令与 `security/credentials.py` 未接 |
| PR-171 审计 | `/v1/audit` + 链A 4 个 action | 工具级/回测级/会话级事件无生产者 |
| PR-143 告警 | 规则/看板就绪 | Alertmanager 仍 `unconfigured` 不投递；promtool/amtool 未跑 |
| PR-161~165 部署 | compose/systemd/备份/蓝绿脚本齐全 | 全部"生产机未验"：TLS、备份恢复、蓝绿切换没实机跑过 |
| PR-214 Risk | 归因/Shadow 真数据 | 单标的路径 `selection` 是退化值 0.0（带 warning，前端已展示）；shadow 拒单率是「规则+熔断」合成值 |
| PR-103/172 前端 | UX 守卫 + 合规页 | 无登录页/授权码流程；令牌手贴且未验签 |

---

## 5. 需要你提供/决策的输入（逐项）

| # | 需要你做的事 | 影响 | 不提供的后果 |
|---|---|---|---|
| **A** | **批准新增依赖 `duckdb` + `pyarrow`**（写入 `pyproject` + 重生成 hash lock） | PR-122 → PR-124 收口、PR-231/232/233 | PIT 全量做不了；回测只能吃 60 根 live 行情，不可复现到快照级 |
| **B** | **批准队列选型**：`temporalio`（推荐）或 `arq`（轻量，需自建重试/DLQ） | PR-201/202/203、PR-213 调度 | 长任务无法异步/续跑/取消；PR-213 只能在 scaffold 上打转 |
| **C** | **磁盘**：当前根分区仅剩 **1.4G**；PR-122 需要放 Parquet 快照 + DuckDB 临时文件 | PR-122、PR-231 回填 | 就算批准依赖也跑不动；建议先腾出 ≥10G（见 §8 清 worktree 可回收 ~600M） |
| **D** | **生产机**：8C/16G 起 + 独立备份盘（README §1.1 已列） | PR-161~165 验收、PR-301~304 | 部署/备份恢复/蓝绿/压测全部停留在"脚本就绪" |
| **E** | **Vault / KMS**（或明确"先不上，接受 .env 明文"） | PR-151 剩余部分 | 密钥仍以明文 .env 为主；provider 只是"将来能换"的接口 |
| **F** | **LLM 预算与模型口径**：月度上限、是否接受当前网关的 latency（p95 ≈ 66s） | PR-112 回归门、PR-252 路由 | 评测只能记 `estimated`/无成本，回归门（p95、单任务成本）无法判定 |
| **G** | **数据源授权**：对外功能前必须采购合规源 | PR-322 | 只能内部研究使用；对外发布有法律风险 |

---

## 6. PR-122 详解（PIT 日线存储与 as-of 查询）

### 6.1 是什么
现在的回测行情是**每次现取**（`tools.get_market_data` → 最近 N 根日线），既不可复现到"当时看到的那个数据版本"，也没有停牌/退市/复权因子的严格时点语义。

PR-122 要落的是**时点数据层**：
- 日线 + 复权因子落 **Parquet（按 `trade_date` 分区）**，用 **DuckDB** 做查询；
- `as_of` 查询**只返回该时点可见的数据**（当时的复权因子、当时的停牌状态、当时还没退市的标的）；
- 交易日历、停牌、复权、退市标记；
- 回测从"读 live"改成"读快照"，快照 id 进 `reproducibility`（补上 PR-124 的最后一块）。

### 6.2 为什么现在卡住
| 卡点 | 证据 |
|---|---|
| 需要新依赖 | `pyproject.toml` 现有 extras 只有 `polars`；`duckdb`/`pyarrow` **都未安装**（`.venv` 实测 `find_spec` 均为 False） |
| 需要磁盘 | 根分区剩 **1.4G**；全市场多年日线 Parquet 不可能塞下 |
| 需要数据口径 | 覆蓋多少年、多少标的、是否含退市股（幸存者偏差）、复权方式，都要先定，否则做完还要返工 |

### 6.3 需要你提供什么
1. **批准依赖**：`duckdb`、`pyarrow`（Approval 后我改 `pyproject.toml` + 用 `uv pip compile --generate-hashes` 重生成 `requirements-lock.txt`，并补 SBOM 与 pip-audit）。
2. **磁盘**：至少 **10G 可用**（估算：A股约 5,000+ 标的 × 10 年日线 ≈ 1,200 万行；Parquet 压缩后粗估 1~3G，加 DuckDB 临时/索引与快照冗余，按 3 倍安全系数；**这是估算，实际以首批样本外推为准**）。
3. **口径确认**（三选一，或直接说"你定"）：
   - 覆蓋范围：全市场 / 沪深300+中证500 / 只做 `600519.SH` 单标的；
   - 时间跨度：3 年 / 10 年；
   - 是否含退市与停牌标的（含 = 能做幸存者偏差检验，但数据量更大）。
4. **数据源**：当前用 akshare/tushare/yahoo（内部研究口径）。若要长期稳定，需要 tushare 积分或采购源——**这个可以后置**，先用现有源做小样本。

### 6.4 验收（来自 phase-1 §4）
- `as_of` 查询只返回时点可见数据（用"未来函数"用例反证）；
- 同一 `snapshot_id + code_commit + config_hash` 重跑，结果 hash 一致（浮点容差 1e-9）；
- 数据缺口/异常能阻断回测并给出 reason（PR-123 已就绪）。

### 6.5 建议的分期（降低一次性投入）
1. **122-a 小样本（1~2 天，不需大磁盘）**：单标的或沪深300、3 年日线落 Parquet + DuckDB as-of 查询 + 用例，先把"口径与代码形状"定死；磁盘约 100M 级。
2. **122-b 扩量**：按 122-a 的样本外推，再决定全市场/10 年与磁盘采购。
3. 完成后补 PR-124 的 `snapshot_id`，PR-231 pgvector 回填才有稳定数据源。

---

## 7. PR-201 ~ PR-203 详解（W4 长任务队列）

### 7.1 是什么
现在长任务（回测、批量研究）是**请求内同步跑**：请求断开就没了，worker 崩了不能续，也取消不了。三件事：

| ID | 交付物 | 验收 |
|---|---|---|
| PR-201 | 长任务队列 + worker | 任务异步执行、失败重试、超限进 DLQ |
| PR-202 | 断点续跑（复用 PG checkpoint）+ 工具调用幂等 | kill worker 后任务能续跑；重复消息只执行一次 |
| PR-203 | 取消 + 进度 | `POST /v1/runs/{id}/cancel`；SSE 推 `run.progress/run.done/run.failed`；取消 5s 内生效 |

数据模型（phase-2 §1 已定）：`runs` 表加 `status/attempt/lease_until/heartbeat_at/error`；worker 拉取 → 执行 AgentLoop → 写结果 → ack；失败按退避重试，超限进 DLQ。

### 7.2 为什么现在卡住
| 卡点 | 证据 |
|---|---|
| 需要新依赖 | `temporalio` 未安装（`.venv` 实测 False）；`arq` 也没有 |
| 需要运维决定 | Temporal 要额外跑 server（compose 多一个服务 + 一套 DB/schema）；Arq/RQ 只要 Redis，但重试/DLQ 要自己写 |
| 本机资源 | 单机 4C/8G + 已有很多容器；多一个 Temporal server 需评估内存 |

### 7.3 需要你提供什么
1. **选型拍板**（二选一）：
   - **A. Temporal**（`temporalio`）：功能最全（重试、超时、续跑、可视化），代价是多一个服务与学习/运维成本；与 `scheduled/` 的既有 scaffold 方向一致。
   - **B. Arq/RQ（Redis）**：轻，部署简单；但重试/DLQ/断点续跑要自己实现（PR-202 工作量会变大）。
2. **批准对应依赖**（A：`temporalio`；B：`arq`），我负责改 pyproject + hash lock + SBOM。
3. **确认部署形态**：Temporal server 是否允许进 `docker-compose.prod.yml`（多一个容器 + 数据卷）；若不允许，只能选 B。
4. **（可选）优先级**：PR-201~203 与 PR-215/241 谁先。我建议 **先 215/241**（无外部依赖，能立刻推进），队列等你批复后再做。

### 7.4 验收（phase-2 §1）
kill worker 后任务能续跑；重复消息只执行一次；取消 5s 内生效；DLQ 可查询/可重放。

---

## 8. worktree 清理（可直接执行，需你点头）

`.codebuddy/worktrees/` 下 **24 个 worktree 分支全部已并入 master**（`git branch --merged master` 全列），只有 3 个存在**未跟踪噪音**（`frontend/node_modules`、`.omc/`），**没有未提交的代码**。

- 占用：`.codebuddy` 共 **637M**（`b8-e2e` 223M、`b10-risk` 208M 是 node_modules 副本）；
- 当前根分区只剩 **1.4G**，清理后可回收到约 **600M+**。

建议命令（我未执行）：

```bash
cd /root/projects/hero-quant
for d in .codebuddy/worktrees/*/; do git worktree remove --force "$d"; done
git worktree prune
# 已合并分支可保留 main 一条，其余删除：
git branch | grep -E '^\+?\s*(worktree-|b1[0-3]-|b[1-9]-)' | tr -d '+ ' | xargs -r git branch -d
```

---

## 9. 下一步顺序建议

**今天就能推进（不需要你给任何东西）**
1. PR-215 管理后台（用户/租户/配额/审计/开关）——依赖 PR-103/171，均已就绪。
2. PR-241 审计不可篡改（链式 hash + 保留策略 + 补齐工具/回测/会话事件的生产者）。
3. PR-234 回测现实性（滑点/停牌/涨跌停/退市/基准）——PR-124 已就绪。
4. PR-151 收尾：Redis 口令接 provider + 轮换事件接审计（小改动）。

**你批复 A/B/C 之后**
5. PR-122-a 小样本 PIT（1~2 天）→ 122-b 扩量 → 补 PR-124 的 `snapshot_id`。
6. PR-201~203 队列（按你选的 Temporal/Arq）。

**等你给生产机**
7. PR-161~165 实机验收（TLS、备份恢复、蓝绿）→ PR-301~304 演练/压测。

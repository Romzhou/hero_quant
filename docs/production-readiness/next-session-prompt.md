你是 hero-quant 的 orchestrator，继续批次 17。仓库：/root/projects/hero-quant（Linux）。

## 先读（按序，不要跳）

1. `AGENTS.md` —— 特别是 §11「完成口径与接线规则」（L1–L4 / 接线测试三层 / 红线三条）
2. `docs/production-readiness/status-2026-11-02-batch16.md` —— 批次16 全量状态；§9 是工作树清单
3. `docs/production-readiness/progress-2026-10-29.md` §20 —— 批次16 收口 + 两条事故（注释不符、同名类）
4. `docs/production-readiness/backlog.md` —— 「未接线/未生效清单（合并审查）」+「L1 但未接线」+ L1–L4 重算
5. `docs/contracts/pr-122b-pit-decision.md` —— 122-b 已定 year_month + 两个前置

## 当前事实（先核实，不要凭记忆）

- 代码 HEAD `a33c81a`（已 push，CI 绿）。**批次16 的 53 个改动仍在工作树未提交**——先 `git status` 确认这些是否已被用户合并；未合并就不要在其上继续堆改动，先问。
- 本地门禁：`scripts/test_local.sh -q` → 3675 passed / 3 skipped；PG 族 `python scripts/run_pg_tests.py` → 66 passed；`ruff check src` 全过。
  **不要直接跑 `pytest -q`**：缺 DSN 会让 PG 族整族 skip（90 skipped），本地绿不代表 CI 绿。
- 完成度口径：只有 L2 及以上算完成；当前 **L2 = 29/67**。
- 批次16 结果：A=L3、B=L1、C=L4；**没有把任何 L1 提升到 L2**。

## 批次 17 任务（顺序固定，不许换序，不许开新线）

1. **【P0】B4 DedupStore 接线**：让工具调用真正幂等。验收 = 队列重试同一任务时副作用（写文件/下单/发通知）只发生一次；必须有一条集成测试：模拟 worker 中途崩溃重跑，断言工具副作用计数 == 1。
2. **【P0】新增 `tests/test_wiring_integrity.py`**（真实装配、非 mock）：断言 `memory_store` / `audit_store` / `dedup_store` / `secrets_provider` / `pricing_provider` 在应用启动后全部非 None，并按 AGENTS §11 做到第二层（`type(x).__module__` 精确匹配）与第三层（行为探针）。
3. **【P1】B3 memory_store 接线**：`server.py` 两处装配点改为注入 `hero_quant.memory.store.MemoryStore`（持久化），不是 `hero_quant.agent.memory.store.MemoryStore`（进程内 LRU）。
4. **【P1】B2 数据源文档**：把「运行时只注册 2 个源（Tencent/Yahoo），16 源白名单只是声明」写进文档，删掉「16 源顺序回退」的表述；恢复多源另开线。
5. **【P2】B1 session 端点**：本批**不做**，只在文档标注「生产者已挂（session_run_hook），端点未实现」。

## 硬约束

- 不许为了让测试变绿而弱化断言（删断言/放宽/加 skip）——最高优先级红线。
- 禁止两个模块定义同名类；注释描述「由谁负责」的机制必须有代码路径佐证。
- 文档引用代码**不写行号**，用函数名/语义锚点/测试名。
- `server.py` 单写者；`db/migrations/` 冻结；新增 env 必须 settings + .env.example + README 三处联动。
- 122-b **不开工**（前置：① 修 `PitParquetStore.as_of` 的 glob 裁剪并重跑基准；② 建 instrument master）。

## 已知工具/环境坑（少走弯路）

- `Edit` 在目标串多处匹配时会**静默不生效**：先断言出现次数唯一，或用 python 脚本按唯一上下文打补丁。
- `Write` 不能覆盖已存在的文件：写提交信息用 `mktemp -u` 生成路径（不要先 `mktemp` 建空文件）。
- CI postgres 镜像已改 `postgres:16-alpine`，**未在 CI 验证**；若红在字符串排序/大小写比较，先怀疑 musl collation，不要改测试。
- 磁盘约 1.1G，实验产物放 /tmp 用完即删。

## 汇报方式

每完成一项**单独汇报**：做了什么（文件+函数）、验证数字（前后对比）、未做/卡住、需要我决策的（≤2 条）。不要攒到最后一次性汇报。

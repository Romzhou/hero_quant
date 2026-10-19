# Runbook：线上运维（就绪判读 / 503 / 存储排查 / 回滚）

> 依据：`docs/recon.md` §3–§4、`docs/issues.md` P0-3/P0-4/P1-2/P2-4。行号为 2026-10-19 快照实测值；本手册只读不改代码，改行为走 D/E/A 线。
> 安全线：禁读/回显 `.env` 真值；DSN 一律脱敏（`dsn_host="***"`，见 `server.py:943,966`）。

## 0. 拓扑速览（先看这个再动手）

- 后端唯一启动：`uvicorn hero_quant.api.server:app --port 8899`（`app` 定义 `server.py:468`；`Dockerfile` CMD 对齐，见 recon §1）。`python -m hero_quant.api.server` 已废弃（P0-2）。
- 依赖：`postgres:16` + `redis:7 volatile-lru` + `otel-collector`（`docker-compose.yml`，recon §1）；单机验收 `docker compose up --build` 后 8899 同源 + `/ready` 绿（AGENTS §3）。
- 主机名规则（P1-4）：本地默认 `localhost`，compose 内必须 `postgres`，经 `settings.py` 统一切换（`settings.py:150,165-171`）。容器内连 `localhost:5432` 必失败——先查这个。

## 1. `/ready` degraded 判读

- 本体：`@app.get("/ready")` 在 `server.py:1194`，docstring（`:1196-1200`）“聚合 degraded… checkpoint 缺配与 billing fail-closed 以 reasons 透出；整体 degraded 时 503 + degraded:true + reason”。
- 判定逻辑：`status="degraded"` 三分支在 `server.py:1229,1231,1234`（PG 应达未达 / Cohere 不健康 / billing 应达未达）；`degraded = status != "ok"`（`:1244`）。
- 探针基础设施：全局复用池 `_READY_POOL`（`:888-889`，`max_workers=4`）+ TTL 缓存（`:896`，5s）+ `SELECT 1` 2s 超时（`:1008,1099`）。P2-4 旧病（每次新建 `ThreadPoolExecutor`）已治；若仍见 readiness 抖动，先看是否为小机 (`mem2g/cpu1.5`) 超时误标（P2-4），再看缓存命中（`_check_checkpoint_pg_full:927` / `_check_billing_pg_full:953` 返回 `cached` 位）。
- reasons速查表：

| reason（含关键词） | 含义 | 出处 | 处置 |
|---|---|---|---|
| `checkpoint PG DSN 缺配 fail-closed(memory)` | `HERO_CHECKPOINT_DSN` 非 PG DSN | `server.py:939`；`checkpoint/postgres.py:284-286` `raise RuntimeError` | 配 DSN 或接受 memory 降级（重启丢检查点，见 P1-2） |
| `checkpoint PG unreachable or not real pool, fail-closed(memory)` | DSN 合法但连不上/非真池 | `server.py:946` | 走 §3 PG 排查 |
| `billing DSN 缺配 fail-closed(memory)` | `HERO_BILLING_DSN` 为空 | `server.py:965`；`settings.py:204` | 配 DSN 或接受无计费路径（ADR-002） |
| `billing PG unreachable or not real pool, fail-closed(memory)` | billing DSN 连不上 | `server.py:970` | 走 §3 PG 排查 |
| `degraded:true + X-Degraded` 头 | 任一降级 | `server.py:183-188` `_with_degraded_headers`；body 例 `:364,391` | 按 reason 分流到 §2/§3 |

## 2. 无 `dist` 503（前端零构建假活，P0-3）

- 现状（待 D 线改行为）：`_resolve_frontend_dist()` 在 `server.py:2424`（候选：`FRONTEND_DIST` env → `/app/frontend/dist` → 仓库 `frontend/dist` → cwd，`:2427-2449`）；缺失时 `_SPA_PLACEHOLDER_HTML`（`:2452-2459`，含 `<!doctype html> + <div id="root">`）经 `_serve_spa_placeholder(:2462)` **恒 200** 返回占位（注释 `:2454` “静态缺失也不 404”，catch-all 注册在所有 API 路由之后 `:2423`）。
- 判读：`/` 返回 200 但正文含 `前端占位（frontend/dist 缺失，API 优先）` → 即假活；健康检查绿不代表前端真活。
- 应急：`npm run build`（`frontend/` 下）产出 `dist` 后后端自动拾取（`_resolve_frontend_dist` 逐请求解析）；或 Docker 多阶段构建保证镜像内含 `dist`。
- 根治（D 语义 + E 消费，不在本手册改）：无 `dist` 时 `/` 明确 503 + 提示 `npm run build`（P0-3 处置）；E 线消费 D 线暴露的语义，不改 `server.py`。

## 3. PG / Redis 不可达排查

### 3.1 PG

1. 看 `/ready` reasons（§1 表）区分“缺配”（配 DSN）还是“不可达”（往下查）。
2. 查主机名：容器内 `localhost` → 改 `postgres`（P1-4；`settings.py:150` 注释保证 compose env）。本地直跑则反之。
3. 查口令硬编码拦截：`checkpoint/postgres.py:277` 对 `postgres:postgres` 默认口令直接 `raise`（“绝不硬编码口令”，`:261-264` docstring）；`settings.py:170-171` 对非法 DSN `warnings.warn + logger.warning`（脱敏 `_redact_dsn`）。
4. 查 emulated 假持久化（P1-2）：若日志见 `pg-emulated` / `skip setup DDL`（`server.py:229-235` 块，实测见 issues P1-2）→ 当前为内存 emulated，**重启丢单/丢检查点**；`emulated` 必须打标并在 `/ready` + `/v1/*` 声明（D 线）。
5. 查 `MissingRunMapping`（`postgres.py:252`）：run 映射丢失时 fail-closed 抛错，禁止用 seq 冒充 run（T2-3 注释 `:251`）——此时不要手动补 seq，去查映射表。

### 3.2 Redis

- 键前缀：`hero:ratelimit:`（`infra/redis.py:26`）、`hero:cache:`（`:27`，链B L2 即此，`ex=600` 见 `server.py:2055`）；入口 `get_redis_sync(:213)` / `get_redis(:281)`，`None` 即不可达。
- 链B fail-open：Redis 挂了回测仍可用（重算直出，`server.py:2127`），但 L2 缓存失效会导致每次重算 + 分布式锁旁路（雷群风险，多 worker 注意）。
- 限流面：`hero:ratelimit:` 丢失 → 签票 gating（`_check_rate_limit → consume_ticket`）可能放行超额，恢复后观察 `REQUEST_COUNTER`（`/ready` 等端点 `labels(endpoint=...)` 打点，`:1204,1578,2187...`）。

## 4. 回滚到上一个 git tag / commit

- 只读确认（先看，不动）：`git log --oneline -5`；`git tag --sort=-creatordate | head -5`；`git status --short`（必须干净，有未提交改动先 `git stash` 并记录）。
- 回滚（ detached 演练，不改分支）：`git checkout <tag-or-commit> --quiet` → 按 §0 启动 → 验 `/ready` + 8899 同源 → 切回：`git checkout master --quiet`（分支名以 `git branch --show-current` 为准，recon 记 `master`）。
- 回滚（动分支，线上事故才用）：`git reset --hard <tag-or-commit>`（分支指针后移；**禁 `git push`**——推送由总管统一处理，本线无 push 权限）。
- 禁区重申：`migrations/` 冻结——回滚若跨越 DDL 版本（`001_checkpoint.sql:5` / `002_billing_rls.sql`），只回代码不回库结构面，库侧变更须专线指令；`.env` 真值不碰、不贴、不传播（P0-1，只轮换）。

## 5. 升级上报（什么情况下不要自己扛）

- `billing` fail-closed 持续（涉及钱）：按 ADR-002 接受降级或配 DSN，不要绕过 `_pg_publish_sync=False` 检查（`service.py:429-444` 注释点名 `test_c_emulated_pg_fail_closed` 兼容语义）。
- `_FakeLLM` 疑似在线上生效（P1-1）：响应体/头查 `degraded`（`server.py:167-188,364,391`）；确认后按演示数口径对外声明，不要当实盘依据。
- 宽 `except` 吞错现场（P1-3 点名 `server.py:381,607-609`、`risk.py:164,236`）：保留 `exc_info` 日志 + 指标计数后转 D 线收窄，不要本地 `except pass` 了事。

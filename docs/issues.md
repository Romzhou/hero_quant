# docs/issues — recon 第4节问题对照表（C线沉淀）

> 依据唯一来源：`docs/recon.md` 第4节（P0×4、P1×4、P2×4、P3×4小项）。
> 本文件是 C 线沉淀：逐项给出 `recon行号 → 实测行号`，行号引用精确到 `文件+行`。
> 验证方式：只读 `read / grep / glob`；`.env` 真值**未读**（安全线禁止），P0-1 以 recon 为准并要求轮换。
> 快照日期：2026-10-19。漂移定义：±5行内为正常演进，仍记“一致（微漂）”并给出实测行；路径变更单列。

## P0 致命（4项，不修不准上线/不准合入）

| # | 主题 | recon 依据 | 实测（文件+行） | 结论 | 处置 / 归属 |
|---|---|---|---|---|---|
| P0-1 | 明文真密钥 + env_file 自动加载 | `.env:3-4`（`HERO_LLM_API_KEY` / `HERO_API_KEY` 疑似真值），`compose env_file` / `Settings` 默认加载 | **未实测**（禁读 `.env` 真值）；`.gitignore:6` 有 `.env` 条目，已确认覆盖 | 一致（安全线不复核真值） | 立即轮换 + 占位符 + 永不入库；归 A 线（`.env.example`）+ 全员 |
| P0-2 | README 错误启动示例 | `README.md:66` `python -m hero_quant.api.server`；反证无 `__main__.py`、无 `__main__` 守卫；对齐 `Dockerfile:103 CMD` | `README.md:66` 为正确 `uvicorn … --port 8899 --reload`，**错误行实为 `README.md:67` `# 或 python -m hero_quant.api.server`**；`src/hero_quant/api/__main__.py` 缺失（glob 无命中）；`server.py` 无 `if __name__`（grep `__main__` 仅命中 `scripts/`）；`Dockerfile:103` 为注释，**CMD 实为 `Dockerfile:104`** `CMD ["python","-m","uvicorn","hero_quant.api.server:app",…]` | **漂移+1行（66→67；103→104），已修正** | 改为 `uvicorn hero_quant.api.server:app --port 8899`，删 `python -m` 行；归 **A 线**（`README.md` 仅 A 可改，C 不碰） |
| P0-3 | 前端零构建本地假活 | `frontend/dist/` 零文件 + `.gitignore` 忽略 `dist`；`server.py:2062-2174` 静态托管 + `_static` 回退假页面 | `frontend/dist/**/*` glob **零命中**；`.gitignore:14` 有 `frontend/dist/`；`server.py:2062-2068` SPA 占位 HTML（含 `<!doctype html>` + `<div id="root">`），`server.py:2147` fallback 注释恒 200，`server.py:2174` `frontend.not_found … serving API only + SPA placeholder`；`server.py:2032-2051` `FRONTEND_DIST` 候选路径 | 一致（范围 2062–2174 覆盖占位+回退+日志） | 无 `dist` 时 `/` 明确 503 + 提示 `npm run build`，或 Docker 多阶段必构建；语义归 **D 线**，消费归 **E 线**（E 不改 `server.py`） |
| P0-4 | 缺配直接 raise（可用性断点） | `src/hero_quant/checkpoint/postgres.py:246-248` 缺配 `raise RuntimeError` | `src/hero_quant/checkpoint/postgres.py:246-248` **逐行一致**：`raise RuntimeError("checkpoint PG DSN 缺配…fail-closed…")`；上游 `server.py:206-254` 探针+回退 `pg-emulated`（`server.py:231-234`、`250-253`） | 一致 | 明确 `fail-closed/fail-open` 并在 `/ready` 如实暴露；归 **D 线**（唯一可改 `server.py` 的线） |

## P1 严重（4项，本迭代必修）

| # | 主题 | recon 依据 | 实测（文件+行） | 结论 | 处置 / 归属 |
|---|---|---|---|---|---|
| P1-1 | `_FakeLLM` 合成演示数无感知 | `src/hero_quant/api/server.py:1091-1401` 无 key 时合成投研结论，前端无标识 | `server.py:1091` `class _FakeLLM:`（第一处），`server.py:1105` `llm = _FakeLLM()`，`server.py:1387` 第二处 `class _FakeLLM:`，`server.py:1401` `llm = _FakeLLM()` | 一致（1091–1401 覆盖两处定义） | 响应头+body 强制 `degraded:true + reason`，前端横幅；归 **D 线** |
| P1-2 | PG 不可达跳 DDL 走 emulated 假持久化 | `src/hero_quant/api/server.py:229-235` 跳 DDL 走内存 `emulated`，探针仍 ready | `server.py:229-235` **逐行一致**：`saver = _Saver(dsn)` + `saver._setup_done = True # skip setup DDL` + `mode="pg-emulated"`；同块延续 `server.py:236-254`（1s TCP 探针 + `ConnectionError … using emulated store` + 二次回退） | 一致（附带说明外层块已扩展至 206–254） | `emulated` 打标并在 `/ready` + `/v1/*` 显式声明；归 **D 线** |
| P1-3 | 宽 except 吞错（三处） | `server.py:381`、`server.py:606`、`risk.py:164,236` 裸 `except Exception` | `server.py:381` `except Exception as _e:` + `ratelimit_failed_503`（已带 warning，仍为宽捕获）；`server.py:607-609` `except Exception … logger.debug("otel.export.failed"…) + pass`（宽捕获+静默）；`risk.py:164`、`risk.py:236` 均为 `except Exception as e:` | 一致 | 收窄异常类型 + `structlog` + 指标计数；归 **D 线**（`server.py`、`risk.py` 仅 D 可改） |
| P1-4 | 双写死 localhost + 旧口令默认 | `settings.py:172` + `server.py:214` 双写死 `localhost`，与 compose `postgres` 矛盾；`README.md:142` 旧口令 | `settings.py:172` `return "postgresql://localhost:5432/hero_quant"`（注释 170–171 声明无口令默认）；`server.py:214` `_host, _port = "localhost", 5432`（DSN 解析回退）；`README.md:142` `postgresql://postgres:postgres@localhost:5432/hero_quant`（“已默认 PG … PG 不可达回退 `memory://`”）；`checkpoint/postgres.py:241-242` 对 `postgres:postgres` 硬编码口令直接 `raise` | 一致 | DSN 经 `settings.py` 统一切换（本地 `localhost` / compose 内 `postgres`），README 口令与 compose 对齐；归 **A 线**（`settings.py` 仅 A 可改） |

## P2 一般（4项，择期修）

| # | 主题 | recon 依据 | 实测（文件+行） | 结论 | 处置 / 归属 |
|---|---|---|---|---|---|
| P2-1 | `server.py` 30+ 处静默 | `grep -n "except.*pass\|logger.debug" server.py` 30+ 命中 | 抽查命中 20+（`server.py:258,275,310,318,340,402,416,424,434,456,462,475,484,495,497,592,603,608,611,629…`），`server.py:609,612` 典型 `logger.debug … + pass # intentional offline-safe` | 一致 | 按链A/链B分级提 `warning/error` + 脱敏；归 **D 线** |
| P2-2 | checkpoint postgres 无结构化字段 | `src/hero_quant/checkpoint/postgres.py` `except` 仅字符串 log | `postgres.py:244-245` `logger.warning("checkpoint 默认 DSN 解析失败，fail-closed: %s", _exc)`（无 `exc_info/error.code/dsn_host`）；`postgres.py:254-256` `except Exception: pass` 裸吞 | 一致 | 统一 `structlog` schema（含 `trace_id/error.code/dsn_host` 脱敏）；归 **D 线** |
| P2-3 | 阈值与“277 passed”口径打架 | `pyproject.toml fail_under=50` vs `README` 30 vs “277 passed” vs `ci.yml` 46 | `pyproject.toml:67` `fail_under = 50`（注释 66 声明 48→50，见 `test.yml`）；`test.yml:53` `--cov-fail-under=50`；`ci.yml:103` `--cov-fail-under=46`（旧值残留）；`README.md:63` `pytest -q # 277 passed`；`README.md:130` `--cov-fail-under=30 # core 30 起步`（recon 所称“README 30”实为此行，另有 63 行历史数） | **一致，补精确行号**：README 旧值在 `:130`（另 `:63` 历史数） | 统一 50，README 更新，CI 以 `test.yml` 为准，归档 `ci.yml` 旧值；归 **B 线** |
| P2-4 | `/ready` 每次新建池双探易误报 | `server.py:729-891` 每次 readiness 新建 `ThreadPoolExecutor` 同步探 PG+Redis | `server.py:792` `with _cf.ThreadPoolExecutor(max_workers=1)`、`server.py:875` `with _cf2.ThreadPoolExecutor(max_workers=1)`（两次同步探针）；`server.py:760,843` `SELECT 1（带 2s 超时…）`；`@app.get("/ready")` 实为 `server.py:930`（装饰器行），处理体延至 930+（recon 的 729–891 覆盖两次探针 helper，`/ready` 本体已下移） | **微漂（探针行一致，`/ready` 装饰器实为 930），已修正** | 复用全局池 + 缓存探针 + 区分 liveness/readiness；归 **D 线** |

## P3 卫生（4小项，顺手修）

| # | 主题 | recon 依据 | 实测（文件+行） | 结论 | 处置 / 归属 |
|---|---|---|---|---|---|
| P3-1 | 无新人契约 | 根 `glob AGENTS* CLAUDE* CONTRIBUTING*` 无命中 | 落点时：根 `AGENTS.md` **已由总管写入**（`git log e74b003 line0-AGENTS`），`CONTRIBUTING.md` 仍缺（本批次新建）；`recon.md:126` 原“无 AGENTS.md”已过期 | 状态变更，已跟进 | `AGENTS.md` 由 C 线校验增补；`CONTRIBUTING.md` 本批次新建；归 **C 线** |
| P3-2 | `ruff` 仅 E,F,W 无 mypy | `pyproject.toml [tool.ruff]` + 无 `[tool.mypy]` | `pyproject.toml:57-61` `[tool.ruff] target-version="py311" line-length=120 / [tool.ruff.lint]`；`grep mypy pyproject.toml` 无 `[tool.mypy]` | 一致 | 是否引入 mypy 待 B/D 定；C 不碰 `pyproject.toml` |
| P3-3 | TODO + 裸 pass | `akshare_loader.py:108 TODO` + `wall_time` 裸 pass + `memory/` 20 处 pass | 路径漂移：**`src/hero_quant/data/loaders/akshare_loader.py:108`** `TODO: confirm akshare unit; if in shares: vol = vol / 100.0`（多了 `loaders/` 一级，行号仍 108）；`memory/lifecycle.py:125,129,133,183,188,204,335,523`、`hierarchy.py:249,262,340`、`store.py:115,229,246,258` 抽查 15+ 处 `pass`（总量级与“20 处”一致） | **路径漂移已修正，行号一致** | 单测锁定一处清理一处，不得新增裸 pass；归 **B 线**（测试锁定）+ D 线（清理） |
| P3-4 | `docs/plans` 被忽略 | `.gitignore` 中 `docs/plans` 条目 | `.gitignore:17-20` 三条：`docs/plans/`、`docs/plans/**`、`docs/plan/`；本地 `docs/plans/` **14 个文件实际存在**（`read docs/plans` 列出 2026-08-20…2026-09-05 共 14 篇），但 `git status` 不跟踪（ignore 生效） | 一致 | 去向见下节；归 **C 线**（本文即显式说明） |

## 附：链路行号（recon §3，供 D/E 引用，C 不改代码）

- 链A问答：`server.py:1235`（`POST /v1/query/ticket`，实测一致）、`server.py:1338`（`event_generator`，实测一致）、`server.py:322`（`app = FastAPI`，实测一致）；`loop.py:102`（`AgentLoop.run`，沿用 recon，未复核源码细节——C 线不碰 `src/`）。
- 链B回测：`server.py:1789/1805/1822/1925`（backtest 四产物，实测一致）、`server.py:1948`（`/v1/trace/events`，实测一致）；`server.py:1721`（bundle helpers，沿用 recon）。
- 静态托管：`server.py:2032-2051`（`FRONTEND_DIST` 候选路径）、`2062-2174`（占位+回退，见 P0-3）。

## 附：`docs/plans` 去向显式说明（验收第4项）

1. `docs/plans/` 在本机存在 14 篇（2026-08-20 至 2026-09-05），均为本地过程计划文档。
2. `.gitignore:17-20` 明确忽略 `docs/plans/`、`docs/plans/**`、`docs/plan/`，注释为 `local plans not for github`——**有意不入库**，不是遗漏。
3. 因此：库内-plan 以 `docs/recon.md` + 本对照表 + 根 `AGENTS.md` + `CONTRIBUTING.md` 为准；本地 `docs/plans/` 仅作个人草稿，不作为评审/合入依据，不同步、不引用、不在 PR 中要求补齐。
4. 若未来某篇 plan 需要入库，必须改名移入 `docs/` 主目录（或 `docs/archive/`）并经 C 线评审，禁止直接解 ignore 整目录。

## 白名单遵守声明

- 本次 C 线仅碰：`docs/`（本文件）、根 `AGENTS.md`（增补，另 commit）、`CONTRIBUTING.md`（新建，另 commit）。
- 未碰：`src/、tests/、frontend/、e2e/、migrations/、pyproject.toml、Dockerfile、docker-compose.yml、.github/、README.md`。`README.md` 归 A 线，`server.py` 归 D 线。
- 未执行：`git push`、删数据、读 `.env` 真值、动 `migrations/`。未粘贴任何密钥。

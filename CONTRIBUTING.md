# CONTRIBUTING.md — hero-quant 新人提交契约（C线）

> 唯一依据：`docs/recon.md` 第5节五线划分 + 第6节 `AGENTS.md` 草案；根 `AGENTS.md` 为执行契约，本文为新人操作版。
> C 线白名单：本文新建 `CONTRIBUTING.md` 合规（根目录新人契约）；`README.md` 归 A 线，本文不重复启动/env 细节，只给跳转。

## 1. 先读三件套（只读，5 分钟）

1. 根 `AGENTS.md`（9 条契约：只读边界 / env gate / 启动验证 / run-verify / 架构 / fail-closed / 风格 / 覆盖率 / PR）。
2. `docs/recon.md` 第4节（P0×4、P1×4、P2×4、P3）+ `docs/issues.md`（逐项 `recon行号 → 实测行号` 对照，含漂移修正）。
3. 本文件第2节（确认自己是哪条线，只能碰白名单）。

## 2. 五线划分（引用自 `docs/recon.md` 第5节，原样引用，C 线不改代码）

| 线 | 方向 | 能碰（白名单） | 不能碰（黑名单） |
|---|---|---|---|
| A 修启动与配置 | settings 网关 + 容器启动 + README 启动章节 | `src/hero_quant/config/settings.py`、`Dockerfile`、`docker-compose.yml`、`.env.example`、`README.md`（仅启动/env 章节） | `server.py`、`checkpoint/`、`telemetry/`、`tests/`、`frontend/`、`e2e/`、`migrations/`、`.github/`、`docs/` |
| B 补测试与CI口径 | 测试补齐 + 覆盖率/CI 单一口径 | `tests/`、`.github/workflows/test.yml`、`.github/workflows/ci.yml`（归档/对齐）、`pyproject.toml`（仅 `[tool.pytest]` / `[tool.coverage]` 段） | `src/`（业务源码一行不碰）、`docs/`、`README.md`、`frontend/`、`e2e/`、`migrations/`、`Dockerfile`、`docker-compose.yml` |
| C 写文档与AGENTS | 摸底沉淀 + 新人契约 | `docs/`、根 `AGENTS.md`、`CONTRIBUTING.md` | `src/`、`tests/`、`frontend/`、`e2e/`、`migrations/`、`pyproject.toml`、`Dockerfile`、`docker-compose.yml`、`.github/`、`README.md` |
| D 重构可观测与异常处理 | 日志脱敏 + 异常收窄 + fail-closed 显式化 | `src/hero_quant/api/server.py`（唯一可改该文件的线）、`risk.py`、`checkpoint/`、`postgres/`、`telemetry/`、`governance/`、`infra/` | `settings.py`、`frontend/`、`e2e/`、`migrations/`、`tests/`、`docs/`、`README.md`、`.github/`、`pyproject.toml`、`Dockerfile`、`docker-compose.yml` |
| E 前端构建与E2E | 前端构建闭环 + E2E 配齐 | `frontend/`、`e2e/`（或 `tests/e2e/`）、`playwright.config.ts` | `src/`、`migrations/`、`docs/`、`README.md`、`AGENTS.md`、`.github/`、`pyproject.toml`、`Dockerfile`、`docker-compose.yml`、`config/` |

冻结：`migrations/` 五线均禁碰。`README.md` 仅 A 可改（C 禁碰，避免同文件冲突）。`server.py` 仅 D 可改。

## 3. 提交规范

- 一次 PR 只做一件事；PR 描述必须列出“能碰/不能碰”文件清单 + 所属线。
- 每段完成就 `git commit`，**勿 `push`**（由总管统一处理）；`git add` 必须带显式路径，禁止 `git add -A`（多线并行会卷入他人文件）。
- 提交信息前缀按线：`A:` / `B:` / `C:` / `D:` / `E:`（例 `C: docs/issues P0-P3 line mapping`）。
- 行号依据：提问/报告必须带 `文件路径+行号+依据`（见 `AGENTS.md` §7）；问题行号以 `docs/issues.md` 实测列为准。

## 4. 安全线（违反直接打回）

- 禁止 `git push`（本批次）、删数据、读/回显 `.env` 真值、动 `migrations/`；不贴任何密钥。
- `HERO_BILLING_DSN` 为空即 fail-closed，不得静默旁路（见 `AGENTS.md` §2）。
- 日志统一 `structlog`，PII/API key 永不进日志（见 `AGENTS.md` §7）。

## 5. `docs/plans` 去向（显式说明）

- `docs/plans/` 被 `.gitignore:17-20` 有意忽略（`local plans not for github`），本地草稿不入库、不作为评审依据。
- 库内计划以 `docs/recon.md` + `docs/issues.md` + `AGENTS.md` + 本文为准。
- 某篇 plan 需入库时：改名移入 `docs/` 主目录并经 C 线评审，禁止直接解 ignore 整目录。

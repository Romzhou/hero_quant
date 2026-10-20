# Agent 评测（real 模式）执行记录

> 线别：T（真模型评测）。日期：2026-10-20。
> 边界：只动 `evals/**` 与本文件；未碰 `src/`、`migrations/`、`.env`；未 push。

## 1. 结论

**`--mode real` = skipped / `no_llm_backend`。本轮不产出任何模型质量结论。**

- 命令：`python evals/run.py --mode real`
- 输出：`evals/results_real.json` → `{"mode": "real", "status": "skipped", "reason": "no_llm_backend", ...}`
- 原因：仓库 `.env` 的 LLM key 为占位值（`change-me`），真实后端返回 `401 invalid_api_key`。
- 证据：跳过 detail 里带原始 401 报文（`Incorrect API key provided: change-me`）。

## 2. 过程中修掉的一个 harness 缺陷（重要）

修复前的 `--mode real` 行为是**错的**：guard 只识别「provider SDK 缺失 → factory 返回内嵌 `_FallbackChat`」这一种离线形态。本次环境里 SDK 已装（`langchain_openai` 可用）、只是 key 无效，于是 38 个用例被真跑一遍，全部以 `empty_text` 失败，脚本把 **headline 0/38** 写进了 `evals/results_real.json`——把「后端不可用」伪装成「模型全线不及格」。

修法（`evals/run.py`，real 分支）：

1. 新增连通性预检 `llm.chat("ping")`：必须回非空文本；
2. 预检不通过 → fail-closed 写 `status=skipped` / `reason=no_llm_backend`（detail 附原始异常）并返回 0；
3. 不再产出任何看似「通过率」的数字。

这正是 P 线报告里写的「real skipped(no_llm_backend) 存证」在当前环境下的正确形态——**没有后端就不给数字**，宁可留白也不伪造。

## 3. fake 基线（未受影响）

`python evals/run.py --mode fake` → headline **30/30 = 100.0%**，known_gap 8/8，`failure_classes={}`；`ruff check evals` 通过。

fake 只证明评测管道与断言自洽，**不证明模型质量**；模型质量结论必须来自 real 模式。

## 4. 台账（不属本线，需 owner）

| # | 事项 | 归属 | 阻塞 |
|---|---|---|---|
| T1 | 注入可用 provider key（`HERO_API_KEY` 或 provider 专属 key）后重跑 `--mode real`，产出真实通过率 + 失败分类 | 需凭据 | key 不可用 |
| T2 | P 线遗留 4 条 loop/prompt 最小修（`server.py:1422` / `:1816` 模板与免责声明、eval 侧 risk_warning 缺口等） | D 线（需改 `src/`） | 超出本线范围 |
| T3 | real 模式按 API 用量计费，**不消耗 Codex 订阅额度**；只以烧订阅额度为目的时可跳过本线，改由 Codex 自行跑 S/R/V | 决策 | — |

## 5. 验收（实跑 2026-10-20）

- `python evals/run.py --mode real` → `[eval] SKIP real 模式：无可用 LLM 后端 (预检未通过)`，写 `evals/results_real.json`（skipped/no_llm_backend）。
- `python evals/run.py --mode fake` → headline 30/30 = 100.0%，known_gap 8/8，`failure_classes={}`。
- `ruff check evals` → All checks passed。

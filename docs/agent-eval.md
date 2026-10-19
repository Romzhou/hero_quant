# Agent 评测集报告（P线）

- 数据集：`evals/dataset.jsonl` — 38 条自然语言投研查询
- Harness：`evals/run.py`，一键重跑见 `evals/README.md`
- 被测链路：链A问答 `server.py -> LLMFactory -> AgentLoop.run(loop.py:616) -> trace.jsonl`；
  fake 口径与 `server.py:1422` 的 `_FakeLLM` 模板一致；工具断言以
  `tools/registry.TOOL_REGISTRY`（18 个已注册工具）为 ground truth
- 本轮无任何 `src/` 修改（只写 `evals/**` + 本报告）；loop/prompt 问题一律只给最小 patch 建议

## 基线（2026-10-19）

| 模式 | 结果 |
| --- | --- |
| `python evals/run.py --mode fake` | headline **30/30 = 100%**；known_gap 8 条在 fake 下全部通过模板检查（缺口记为 spec-only notes，非失败） |
| `python evals/run.py --mode real` | **skipped**（原因：`no_llm_backend`，见下） |

用例分布：A 市场行情 6 / B 基本面 5 / C 指标因子 6 / D 相关组合 4 / E 回测 8 / F 边界降级 9。
每条含 `id / query / expected_tools / expected_structure / must_contain`，
三层断言：工具名必须真实注册；结构 markers（`FAKE_CHECKABLE` 映射）；
关键数字（`600519.SH / 1680.2 / sharpe / 1.62 / grounding_verified / 演示模式`）必须出现。

## 失败分类

本轮 fake headine 失败数 **0**，分类计数全空（`failure_classes: {}`）。
8 条 `fake_expect=known_gap`（D03、E01、E04、E06、F02、F03、F04、F07）
在 fake 下的缺口全部落在三类 spec-only 结构上，
已由 harness 记为 `notes` 而非失败：

- `risk_warning:absent_in_fake` — D03/E01/E04/E06：fake 模板从不输出风险提示，
  与 fail-closed展示要求（降级必须显式）存在缺口；
- `refusal_or_clarify:absent_in_fake` — F02/F03/F04：模糊指代、prompt 注入、
  精确预测未来价三类 fake 均直接回模板，无澄清/拒绝分支；
- `tool_trace:absent_in_fake` — F07：fake 路径零工具调用，`trace.jsonl` 无 tool span，
  多工具编排类查询在 fake 下只能做静态路由断言，无法动态验证。

`results_fake.json / results_real.json` 为本轮产物，可直接比对复跑。

## 真 LLM 状态

`--mode real` 已尝试：`LLMFactory.create()` 返回外层 `LLMClient`，
内层经递归内省确认为 `_FallbackChat`（`src/hero_quant/llm/factory.py:147`，
`langchain_openai` 未安装，日志 `ChatOpenAI not installed, using fallback`）。
harness 判定为离线 fallback，按 `skipped(no_llm_backend)` 退出码 0 落盘，
未发起任何网络调用。有真实后端后重跑同一命令即可得到真基线；
real 模式只做连通性健康断言（terminated / 非空文本），数字只记录 hit/miss 不判失败。

## loop/prompt 最小修建议（仅建议，未落地，需 D 线确认）

1. 两处 `_FakeLLM` 模板漂移 — `src/hero_quant/api/server.py:1422`（问答路径，
   短模板）与 `src/hero_quant/api/server.py:1816`（stream 路径，多出
   `数据来源 tencent(synthetic) / 回测区间 2026-07-20~2026-08-12 / 结论：等权策略跑赢基准` 三行）。
   建议：抽成同一常量，两处复用；本 eval 的 `FakeLLM` 以 `:1422` 为准，
   若 D 线统一为长模板，同步更新 `evals/run.py::FAKE_CHECKABLE`。
2. fake 模板原样回显用户 query（`:1422`/`:1816` 的 `for query: {goal}`）：
   F03 类注入语句会被逐字回显。建议：fake 回显前对 goal 做截断+引号包裹
   （纯展示层改动，约 3 行），或在 eval F03 上加 `must_not_contain` 反向断言。
3. fake 模板缺风险提示行：建议在模板尾追加固定 disclaimer
   （如 `本结果为合成占位，不构成投资建议`，1 行），可一次性补上
   D03/E01/E04/E06 的 `risk_warning` 缺口；属 D 线展示口径，需 D 线落地。
4. 以上三项均在 `server.py`（D 线文件）内，P 线按约束不碰，只列路径行号与依据。

## 验收

- 一键重跑：`python evals/run.py --mode fake`（退出码 0，基线 headline 30/30=100%）
- `ruff check evals` 通过，`ruff format --check evals/run.py` 通过
- 真 LLM：跳过，原因 `no_llm_backend`（`evals/results_real.json` 存证）

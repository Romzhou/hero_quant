# P线 Agent 评测集

30+ 条自然语言投研查询，断言“该调哪个工具 / 该产出什么结构 / 哪些数字必须出现”。

## 一键重跑

```bash
python evals/run.py --mode fake   # 无需 key，FakeLLM + AgentLoop，基线
python evals/run.py --mode real   # 需真实 LLM 后端；缺 SDK/key 时自动跳过并注明
```

## 文件

- `dataset.jsonl` — 38 条用例（JSONL，每行一个 case）。
  字段：`id / category / query / expected_tools / expected_structure / must_contain / fake_expect`。
  分类：A 市场行情(6) / B 基本面(5) / C 指标因子(6) / D 相关组合(4) /
  E 回测(8) / F 边界降级(9)。
- `run.py` — 评估入口，三层断言：
  1. `expected_tools` ⊆ `tools/registry.TOOL_REGISTRY`（防幻觉工具名）；
  2. `expected_structure` markers（fake 可验证部分以 `FAKE_CHECKABLE` 映射检查；
     `risk_warning / refusal_or_clarify / tool_trace` 为 spec-only，fake 下只记录不判失败）；
  3. `must_contain` 数字/标注子串必须出现在 `AgentLoop.run()` 输出中。
  `fake_expect=known_gap` 的 8 条用例预期在 fake 下缺 spec-only 结构，
  不计入 headline 通过率，计入失败分类。
- `results_fake.json / results_real.json` — 最近一次跑出的基线产物。

## 链路归属

fake LLM 模板与 `src/hero_quant/api/server.py::_FakeLLM` 同口径；
被测循环为 `AgentLoop.run`（`src/hero_quant/agent/loop.py:616`），
工具来自 `tools/registry`，不断链、不碰线上数据。

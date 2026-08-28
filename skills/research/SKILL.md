---
name: research
description: 投研研究技能 — 覆盖行情拉取、PIT校验、回测执行与归因分析的端到端工作流
---

# Research Skill

面向 HeroQuant 的投研研究技能，封装从自然语言到回测报告的完整链路。

## 何时使用
- 用户请求“回测 600519.SH 近一月等权”等策略验证
- 需要多市场行情、基准对照、收益归因
- 触发 tearsheet 产出与证据链校验

## 工作流
1. **行情**：经 `MarketDataRegistry` 拉取 `tencent`/`yahoo`，记录 `provenance{source,unit}`，跨源 1% 阻断
2. **校验**：PIT `weights_on ≤ price_date`，非正价格/混币种拒绝
3. **回测**：`BacktestEngine` 执行等权/自定义权重，产出 `positions.csv / metrics.json / tearsheet.html`
4. **归因**：月度热力、回撤 TopN、换手计费

## 工具
- `get_market_data` / `run_backtest` / `technical_indicators` / `quantlib_call`
- `mcp/router` TopK5 融合检索 + `CohereReranker` 精排

## 输出
- 指标：`sharpe / annual_return / max_drawdown / turnover`
- 产物：`positions.csv / fills.csv / metrics.json / tearsheet.html`
- 审计：`GroundingLedger` 证据链 + `ledger.verify()`

## 注意事项
- live 模式下禁止合成回退，失败显式 `RuntimeError`
- 所有价格引用必须通过 `GroundingLedger.assert_price` 校验

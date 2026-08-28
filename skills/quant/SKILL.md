---
name: quant
description: 量化因子与风控技能 — 技术指标、风控熔断、归因与对账
---

# Quant Skill

量化研究与风控执行技能，覆盖因子计算、熔断治理与对账。

## 何时使用
- 计算 `sma/ema/rsi/bollinger/macd/max_drawdown`
- 触发风控：PIT / cross_source 1% / 熔断双桶 / Grounding
- 执行归因与 `ShadowAccount` 对账

## 能力
- **因子库**：`sma ema rsi(Wilder EWM) bollinger macd` 纯 pandas，已对齐 8-11 RSI 修复；Rust `crates/quantlib` 加速可选
- **风控**：`CircuitBreaker 50%/30s/open30s/half5s` + `OTel 80%` 双桶；`BudgetBreaker $5/日`；`RetryPolicy` 指数退避
- **对账**：`ShadowJournal` attribution / coverage，`governance/reconcile` 日跑

## 调用示例
```python
from hero_quant.tools.registry import TOOL_REGISTRY
TOOL_REGISTRY["technical_indicators"].func(symbol="600519.SH", indicator="rsi", window=14)
```

## 输出
- 结构化指标 JSON + `ledger.verify()` 可追溯
- 熔断状态 `CLOSED/OPEN/HALF_OPEN` + `reject_rate`
- 证据链 `trace.jsonl` + `X-Request-ID` 全链路

## 约束
- 读工具 `is_concurrency_safe=True` 可并发，写工具串行
- `HARD RULE` 限定价格仅可引用证据块

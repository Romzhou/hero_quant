# 金融合规与披露 · COMPLIANCE

> 版本 0.2.0 · 适用范围 `hero-quant` 投研 Agent（自然语言→行情→回测→报告）

本文件为产品级合规说明，不构成法律/投资建议。部署与使用方需自行评估并遵守所在地监管要求。

## 1. 投资免责声明（Investment Disclaimer）

- **非投资建议**：本系统输出的行情解读、指标计算、回测结果、报告与自然语言回答仅供研究与教育参考，不构成任何证券、基金、期货、外汇或加密资产的投资建议、要约或承诺。
- **模型局限**：LLM 可能产生幻觉或过时信息；已引入 Grounding 证据账本与 provenance 标注，但仍需人工复核关键结论。
- **自主决策**：任何交易决策由用户自主做出，开发与运营方不对盈亏承担责任。建议在实盘前进行独立尽调与小规模验证。

## 2. 数据许可与来源（Data License & Attribution）

- **行情源**：A股 `tencent (qt.gtimg.cn)` 为主、美股 `yahoo (yfinance)` 为主；注册表登记 16 源，但**数据层没有熔断器**——`CircuitBreaker` 只存在于 `telemetry/circuit.py`，消费方仅有 `shadow/service.py` 与 `mcp/router.py`；`data/registry.py` 的 `get_bars` 是按注册顺序回退，**无打分、无"择优"逻辑**。因此不存在"熔断后自动切到最优源"的能力，请按"顺序回退"理解。可选 `tushare / akshare / ccxt` 需用户自备合规许可与配额。
- **单位与溯源**：A股以 `board_lots`（手）、美股以 `shares`（股）全链路 `provenance{source,unit,date}` 记录；跨源收盘价 1% 阈值校验**代码已实现，但默认双源市场/单位互斥，生产路径不触发**——`_cross_source_check` 只在同一标的拿到两个可比源时才执行，而 CN/US 双源的市场与 unit（`board_lots` vs `shares`）本就互斥，命中即 fail-closed 阻断。
- **合成模式**：`HERO_DATA_MODE=synthetic` **可用**（不再硬失败），但产物会显式打标：`provenance.source=synthetic`、A股 `unit=board_lots`（沿用主源 unit 声明），跨源 1% 比对被**显式跳过并打 warning**（`cross_source_synthetic_skipped`）；synthetic 与 live 混用时需显式 opt-in，否则 fail-closed 拒绝。合成序列不代表真实行情。
- **使用约束**：用户需遵守各数据提供方的服务条款、速率限制与再分发限制。
- **引用要求**：对外发布基于本系统的结论时，应注明数据来源、时间区间与 `HERO_DATA_MODE`。

## 3. 风险揭示（Risk Disclosure）

- **市场风险**：证券价格波动、流动性、汇率、杠杆及衍生品等可能导致本金损失；回测收益不预示未来表现。
- **数据与回测局限**：存在停牌、除权除息、汇率与时区、滑点与费用（按换手计费）等偏差；PIT 校验（`weights_on ≤ price_date`）仅降低未来函数风险，不完全消除幸存者偏差。
- **系统与运营风险**：单机部署、网络抖动、LLM 重试/预算断路（`agent/policies.py`）、PG 不可达时 checkpoint 回退 `memory` 或 emulated 桩、Redis 缺失时限流 fail-closed 等，均可能导致延迟、功能不可用或结果不一致；建议结合监控 `/metrics` 与日志 `X-Request-ID` 巡检。
- **合规风险**：量化策略发布、投顾与资管业务在多地需持牌；用户需自行履行适当性、信息披露与风控义务。

## 4. 隐私与令牌（Privacy & Token）

- **令牌存储**：登录令牌（Bearer JWT）只保存在**本机浏览器** `localStorage`（键 `hero-quant.token`），不上传任何第三方、不写入服务端日志；设置页「清除令牌」即恢复匿名状态。
- **前端解析不构成授权**：前端只解码 JWT payload 展示租户/角色，**不验签、不裁决权限**；401/403 一律由后端裁决（`src/hero_quant/auth/`）。前端路由守卫仅是 UX 提示，不是安全边界。
- **数据边界**：问答 prompt/工具结果会发送到配置的 LLM 提供方（`HERO_LLM_PROVIDER` / `HERO_LLM_BASE_URL`）；部署方需确认该提供方的数据处理条款。日志只记录 `trace_id`/计数/成本，敏感字段按 `security/redaction.py` 脱敏。
- **删除与保留**：当前版本**没有**对外提供“删除我的数据/导出/令牌撤销”接口；会话与审计数据的保留策略见 PR-241（未完成），在此之前由部署方手工执行库级清理。

## 5. 适用范围与责任边界

- 本系统面向投研辅助与自动化报告，不替代持牌投顾与风控流程；生产环境建议叠加人工复核、权限分级与审计（`governance/ledger` Hash 链 + `dedup` 幂等）。
- 如将输出用于对外服务、客户报告或交易执行，部署方应补充法律审查、隐私与数据跨境合规评估。

## 6. 参考与更新

- 相关实现：`src/hero_quant/data/registry.py`（16 源注册表）、`src/hero_quant/backtest/validation.py`（PIT）、`src/hero_quant/governance/ledger.py`（审计链）。
- 本文件随版本迭代更新，重大变更将在 `CHANGELOG` 与 `README` 中公告。
- 产品内入口：前端 `/compliance` 页（与本文同步；版本见页面底部）。

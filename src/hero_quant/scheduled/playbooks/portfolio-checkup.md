---
name: portfolio-checkup
title: 体检
title_cn: 体检
cron: "0 9 * * 1"
timezone: "Asia/Shanghai"
schedule: "Temporal Cron 0 9 * * 1 Asia/Shanghai"
tags: [portfolio, checkup, weekly]
temporal: true
---

# 体检 / Portfolio Checkup — 09:00 Asia/Shanghai Monday weekly

## 目标
每周一 09:00（Asia/Shanghai）对持仓组合做健康度体检：收益/回撤/暴露/集中度。

## Temporal Cron
```yaml
cron: "0 9 * * 1"
timezone: "Asia/Shanghai"
```

## 输入
- ShadowJournal attribution 5类归因
- 风险引擎持仓
- 因子暴露

## 输出
- 组合体检报告（收益归因/风险预警/调仓建议）

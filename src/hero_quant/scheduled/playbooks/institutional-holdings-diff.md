---
name: institutional-holdings-diff
title: 13F异动
title_cn: 13F异动
cron: "0 18 * * 5"
timezone: "America/New_York"
schedule: "Temporal Cron 0 18 * * 5 America/New_York"
tags: [13F, holdings, institutional, diff]
temporal: true
---

# 13F异动 / Institutional Holdings Diff — 18:00 America/New_York Friday weekly

## 目标
每周五 18:00 ET（America/New_York）扫描 13F 机构持仓异动，对比上一季。

## Temporal Cron
```yaml
cron: "0 18 * * 5"
timezone: "America/New_York"
```

## 输入
- SEC 13F 持仓
- 上季快照 diff

## 输出
- 机构加/减仓榜
- 异动因子提示（需 Grounding 校验）

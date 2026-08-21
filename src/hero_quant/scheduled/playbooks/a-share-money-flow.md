---
name: a-share-money-flow
title: 资金流
title_cn: 资金流
cron: "30 15 * * 1-5"
timezone: "Asia/Shanghai"
schedule: "Temporal Cron 30 15 * * 1-5 Asia/Shanghai"
tags: [money-flow, A-share, northbound]
temporal: true
---

# 资金流 / A-Share Money Flow — 15:30 Asia/Shanghai weekdays

## 目标
A股收盘后 15:30（Asia/Shanghai）资金流复盘：北向/主力/板块资金。

## Temporal Cron
```yaml
cron: "30 15 * * 1-5"
timezone: "Asia/Shanghai"
```

## 输入
- AKShareLoader 北向资金
- 关联板块成交

## 输出
- 资金流日报（流入/流出榜、板块资金异动）

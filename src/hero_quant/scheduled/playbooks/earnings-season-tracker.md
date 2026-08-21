---
name: earnings-season-tracker
title: 财报季
title_cn: 财报季
cron: "0 9 * * 1-5"
timezone: "America/New_York"
schedule: "Temporal Cron 0 9 * * 1-5 America/New_York"
tags: [earnings, US, tracker]
temporal: true
---

# 财报季 / Earnings Season Tracker — 09:00 America/New_York weekdays

## 目标
美股财报季 09:00 ET（America/New_York, ZoneInfo DST-aware）自动跟踪当日财报/指引。

## Temporal Cron
```yaml
cron: "0 9 * * 1-5"
timezone: "America/New_York"
```

## ZoneInfo DST 注意
America/New_York 需 DST 自动切换（EST/EDT），ZoneInfo next_trigger 保证夏令时前后仍 09:00 wall time。

## 输出
- 当日财报日历+预期/实际对比
- 盘前异动提醒

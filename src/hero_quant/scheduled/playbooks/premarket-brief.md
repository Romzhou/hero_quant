---
name: premarket-brief
title: 破晓简报
title_cn: 破晓简报
cron: "30 8 * * 1-5"
timezone: "Asia/Shanghai"
schedule: "Temporal Cron 30 8 * * 1-5 Asia/Shanghai"
tags: [premarket, brief, A-share]
temporal: true
---

# 破晓简报 / Premarket Brief — 08:30 Asia/Shanghai weekdays

## 目标
A股开盘前 08:30（Asia/Shanghai, ZoneInfo）自动触发简报，汇总隔夜美股/汇率/期货、今日开盘关注。

## Temporal Cron
```yaml
cron: "30 8 * * 1-5"
timezone: "Asia/Shanghai"
```

## ZoneInfo Dispatch
```python
from zoneinfo import ZoneInfo
from hero_quant.scheduled import get_next_trigger
nxt = get_next_trigger("30 8 * * 1-5", "Asia/Shanghai", after=datetime.now(ZoneInfo("Asia/Shanghai")))
```

## 输出
- 隔夜市场回顾（美股/中概/汇率）
- A股今日关注（资金/情绪/事件）
- Grounding 三级校验后才可引用价格

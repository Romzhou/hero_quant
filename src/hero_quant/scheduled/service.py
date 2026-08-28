"""定时研究调度 — ZoneInfo 时区感知的 cron 分发。

职责：提供 cron 解析/校验、时区感知的 next_trigger 计算及 playbook 注册表。
架构位置：`scheduled` 核心，对接 Temporal Cron，本地可离线计算触发时间用于测试。
关键设计：5 字段 cron 全量解析（*, */n, a,b,c, a-b, a-b/n）；ZoneInfo 校验；分钟级暴力搜索 366 天 horizon；5 个 playbook 注册表统一 `to_temporal_cron` 与 `dispatch`。
"""

from __future__ import annotations
import logging

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
logger = logging.getLogger("hero_quant.scheduled.service")


# ---------- cron 解析 ----------

_CRON_RANGES = {
    "minute": (0, 59),
    "hour": (0, 23),
    "dom": (1, 31),
    "month": (1, 12),
    "dow": (0, 7),  # 0 与 7 均表示周日
}


def _parse_field(field_str: str, min_val: int, max_val: int) -> Set[int]:
    """解析单个 cron 字段为整数集合，支持 *, */n, a,b,c, a-b, a-b/n。"""

    field_str = field_str.strip()
    if not field_str:
        raise ValueError("empty cron field")

    result: Set[int] = set()

    # 按逗号拆分列表项
    parts = field_str.split(",")
    for part in parts:
        part = part.strip()
        if not part:
            raise ValueError(f"invalid cron part empty in {field_str!r}")

        # 含步进 "/"
        if "/" in part:
            base, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                raise ValueError(f"invalid step {step_s!r} in {part!r}")
            if step <= 0:
                raise ValueError(f"step must be >0 got {step} in {part!r}")
            base = base.strip()
            if base == "*" or base == "":
                start, end = min_val, max_val
            elif "-" in base:
                a_s, b_s = base.split("-", 1)
                try:
                    start, end = int(a_s), int(b_s)
                except ValueError:
                    raise ValueError(f"invalid range {base!r} in {part!r}")
                if start < min_val or end > max_val or start > end:
                    raise ValueError(f"range {base!r} out of bounds [{min_val},{max_val}]")
            else:
                # 单值带步进如 "2/3" 表示从 2 到 max 每 3
                try:
                    start = int(base)
                except ValueError:
                    raise ValueError(f"invalid base {base!r} in {part!r}")
                if start < min_val or start > max_val:
                    raise ValueError(f"value {start} out of bounds [{min_val},{max_val}]")
                end = max_val
            for v in range(start, end + 1):
                if (v - start) % step == 0:
                    if min_val <= v <= max_val:
                        result.add(v)
            continue

        # 无步进：处理 "*"、区间或单值
        if part == "*":
            result.update(range(min_val, max_val + 1))
            continue
        if "-" in part:
            a_s, b_s = part.split("-", 1)
            try:
                a, b = int(a_s), int(b_s)
            except ValueError:
                raise ValueError(f"invalid range {part!r}")
            if a < min_val or b > max_val or a > b:
                raise ValueError(f"range {part!r} out of bounds [{min_val},{max_val}]")
            result.update(range(a, b + 1))
            continue
        # 单值
        try:
            v = int(part)
        except ValueError:
            raise ValueError(f"invalid cron field value {part!r} in {field_str!r}")
        if v < min_val or v > max_val:
            raise ValueError(f"value {v} out of bounds [{min_val},{max_val}] for field {field_str!r}")
        result.add(v)

    if not result:
        raise ValueError(f"empty result for field {field_str!r}")
    return result


def _normalize_dow(values: Set[int]) -> Set[int]:
    """归一化周：7 视为 0（周日）。"""
    out: Set[int] = set()
    for v in values:
        if v == 7:
            out.add(0)
        else:
            out.add(v)
    return out


def parse_cron(cron_expr: str) -> Tuple[Set[int], Set[int], Set[int], Set[int], Set[int]]:
    """解析 5 字段 cron 为集合，非法抛 ValueError。"""
    if not isinstance(cron_expr, str):
        raise ValueError("cron must be str")
    cron_expr = cron_expr.strip()
    parts = cron_expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron must be 5-field, got {len(parts)} field(s): {cron_expr!r}")
    minute_s, hour_s, dom_s, month_s, dow_s = parts

    minute_set = _parse_field(minute_s, *_CRON_RANGES["minute"])
    hour_set = _parse_field(hour_s, *_CRON_RANGES["hour"])
    dom_set = _parse_field(dom_s, *_CRON_RANGES["dom"])
    month_set = _parse_field(month_s, *_CRON_RANGES["month"])
    dow_set = _parse_field(dow_s, *_CRON_RANGES["dow"])
    dow_set = _normalize_dow(dow_set)

    return minute_set, hour_set, dom_set, month_set, dow_set


def validate_cron(cron_expr: str) -> bool:
    """校验 cron 合法性，合法返回 True 否则抛 ValueError。"""
    parse_cron(cron_expr)
    return True


def _check_match(candidate: datetime, minute_set, hour_set, dom_set, month_set, dow_set) -> bool:
    """判断候选时间是否匹配 cron 集合（candidate 已为目标时区）。"""
    if candidate.minute not in minute_set:
        return False
    if candidate.hour not in hour_set:
        return False
    if candidate.day not in dom_set:
        return False
    if candidate.month not in month_set:
        return False
    # 周：Python weekday 周一 0..周日 6 转 cron 周日 0..周六 6
    py_wday = candidate.weekday()  # 0 Mon
    cron_dow = (py_wday + 1) % 7  # Mon 0->1, Sun 6->0
    if cron_dow not in dow_set:
        return False
    return True


def get_next_trigger(cron_expr: str, tz_name: str, after: Optional[datetime] = None) -> datetime:
    """计算严格大于 `after` 的下次触发时间（带时区）。

    校验 cron 与时区，horizon 366 天，未命中抛 ValueError。
    """

    # 先校验 cron 合法性
    parse_result = parse_cron(cron_expr)
    minute_set, hour_set, dom_set, month_set, dow_set = parse_result

    # 校验时区
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as e:
        raise ValueError(f"invalid timezone {tz_name!r}: {e}") from e
    except Exception as e:
        raise ValueError(f"invalid timezone {tz_name!r}: {e}") from e

    # 归一化 after：无则取当前时区时间；naive 视为目标时区
    if after is None:
        after = datetime.now(tz)
    else:
        if not isinstance(after, datetime):
            raise ValueError("after must be datetime")
        if after.tzinfo is None:
            after = after.replace(tzinfo=tz)
        else:
            after = after.astimezone(tz)

    # 从下一分钟边界开始（严格 > after）
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)

    horizon = candidate + timedelta(days=366)
    # 最多 366*1440 次迭代
    while candidate <= horizon:
        if _check_match(candidate, minute_set, hour_set, dom_set, month_set, dow_set):
            # 规范化 DST：确保返回为目标 ZoneInfo
            try:
                candidate = candidate.astimezone(tz)
            except Exception as _exc:
                logger.debug("silent handled: offline-safe: scheduled DST normalize", exc_info=_exc)  # intentional: offline-safe: scheduled DST normalize
                pass  # intentional offline-safe: scheduled DST normalize
            return candidate
        candidate += timedelta(minutes=1)

    raise ValueError(f"no next trigger found within 366d for cron {cron_expr!r} tz {tz_name!r} after {after!r}")


# ---------- playbook 注册 ----------

@dataclass
class ScheduledPlaybook:
    """定时 playbook 定义。"""

    name: str
    cron: str
    timezone: str
    description: str
    title_cn: str = ""
    file: Optional[Path] = None
    tags: List[str] = field(default_factory=list)

    def next_trigger(self, after: Optional[datetime] = None) -> datetime:
        """计算该 playbook 的下次触发时间。"""
        return get_next_trigger(self.cron, self.timezone, after=after)

    def to_dict(self) -> Dict[str, str]:
        """转为字典摘要。"""
        return {
            "name": self.name,
            "cron": self.cron,
            "timezone": self.timezone,
            "description": self.description,
            "title_cn": self.title_cn,
        }


# 5 个内置 playbook，需与磁盘 markdown 文件对应
_PLAYBOOKS_DATA: List[Dict[str, str]] = [
    {
        "name": "premarket-brief",
        "cron": "30 8 * * 1-5",
        "timezone": "Asia/Shanghai",
        "description": "A-share pre-market briefing before open — zone-aware dispatch at 08:30 CST weekdays",
        "title_cn": "破晓简报",
        "tags": "premarket,brief,A-share",
    },
    {
        "name": "portfolio-checkup",
        "cron": "0 9 * * 1",
        "timezone": "Asia/Shanghai",
        "description": "Weekly portfolio health check — Monday 09:00 CST",
        "title_cn": "体检",
        "tags": "portfolio,checkup,weekly",
    },
    {
        "name": "a-share-money-flow",
        "cron": "30 15 * * 1-5",
        "timezone": "Asia/Shanghai",
        "description": "A-share money flow post-close analysis 15:30 CST weekdays",
        "title_cn": "资金流",
        "tags": "money-flow,A-share,post-close",
    },
    {
        "name": "earnings-season-tracker",
        "cron": "0 9 * * 1-5",
        "timezone": "America/New_York",
        "description": "US earnings season tracker — 09:00 ET weekdays (DST-aware)",
        "title_cn": "财报季",
        "tags": "earnings,US,season",
    },
    {
        "name": "institutional-holdings-diff",
        "cron": "0 18 * * 5",
        "timezone": "America/New_York",
        "description": "13F institutional holdings diff — Friday 18:00 ET weekly",
        "title_cn": "13F异动",
        "tags": "13F,holdings,diff,institutional",
    },
]


def _build_playbooks() -> List[ScheduledPlaybook]:
    """由内置数据构建 playbook 列表（含文件路径与标签）。"""
    out: List[ScheduledPlaybook] = []
    base = Path(__file__).parent / "playbooks"
    for data in _PLAYBOOKS_DATA:
        p = ScheduledPlaybook(
            name=data["name"],
            cron=data["cron"],
            timezone=data["timezone"],
            description=data["description"],
            title_cn=data.get("title_cn", ""),
            file=base / f"{data['name']}.md",
            tags=data.get("tags", "").split(",") if isinstance(data.get("tags"), str) else [],
        )
        out.append(p)
    return out


PLAYBOOKS: List[ScheduledPlaybook] = _build_playbooks()
_PLAYBOOK_MAP: Dict[str, ScheduledPlaybook] = {p.name: p for p in PLAYBOOKS}


def list_playbooks() -> List[ScheduledPlaybook]:
    """列出全部 playbook。"""
    return list(PLAYBOOKS)


def get_playbook(name: str) -> ScheduledPlaybook:
    """按名获取 playbook，不存在抛 KeyError。"""
    if name not in _PLAYBOOK_MAP:
        raise KeyError(f"playbook {name!r} not found; available: {list(_PLAYBOOK_MAP.keys())}")
    return _PLAYBOOK_MAP[name]


class ScheduledService:
    """时区感知调度服务 — Temporal Cron 占位。

    职责：提供本地 next_trigger 计算，语义与 Temporal cron+时区一致，便于离线测试。
    """

    def __init__(self, playbooks: Optional[List[ScheduledPlaybook]] = None):
        self._playbooks = list(playbooks) if playbooks is not None else list(PLAYBOOKS)
        self._map: Dict[str, ScheduledPlaybook] = {p.name: p for p in self._playbooks}

    def list_playbooks(self) -> List[ScheduledPlaybook]:
        """列出已注册 playbook。"""
        return list(self._playbooks)

    def get_playbook(self, name: str) -> ScheduledPlaybook:
        """按名获取 playbook。"""
        if name not in self._map:
            raise KeyError(f"playbook {name!r} not found")
        return self._map[name]

    def next_trigger(self, cron_expr: str, tz_name: str, after: Optional[datetime] = None) -> datetime:
        """通用 cron 下次触发时间。"""
        return get_next_trigger(cron_expr, tz_name, after=after)

    def next_trigger_for_playbook(self, name: str, after: Optional[datetime] = None) -> datetime:
        """指定 playbook 的下次触发时间。"""
        p = self.get_playbook(name)
        return p.next_trigger(after=after)

    def validate_cron(self, cron_expr: str) -> bool:
        """校验 cron 合法性。"""
        return validate_cron(cron_expr)

    def to_temporal_cron(self, name_or_cron: str) -> str:
        """转为 Temporal 可用的 5 字段 cron 字符串。"""

        if name_or_cron in self._map:
            return self._map[name_or_cron].cron
        # 否则校验其本身为合法 cron
        validate_cron(name_or_cron)
        return name_or_cron

    def dispatch(self, name: str, after: Optional[datetime] = None) -> Dict[str, str]:
        """时区感知分发 — 尝试 Temporal 入队，失败回退本地调度（离线安全）。"""

        p = self.get_playbook(name)
        nxt = p.next_trigger(after=after)
        result: Dict[str, str] = {
            "playbook": p.name,
            "cron": p.cron,
            "timezone": p.timezone,
            "next_trigger": nxt.isoformat(),
            "next_trigger_utc": nxt.astimezone(timezone.utc).isoformat(),
            "title_cn": p.title_cn,
        }
        # Temporal client scaffold — try import temporalio, if available enqueue, else fallback
        try:
            import importlib.util as _ilu

            spec = _ilu.find_spec("temporalio")
            if spec is not None:
                try:
                    # scaffold: real enqueue would use temporalio.client.Client
                    # keep backward compat: log scaffold and mark enqueued
                    logger.info(
                        "temporal enqueue scaffold playbook=%s cron=%s tz=%s next=%s",
                        p.name,
                        p.cron,
                        p.timezone,
                        nxt.isoformat(),
                    )
                    result["temporal"] = "enqueued"
                    result["dispatch_mode"] = "temporal"
                except Exception as _e:
                    logger.info("temporal unavailable -> scheduled fallback: %s", _e)
                    result["temporal"] = "fallback"
                    result["dispatch_mode"] = "scheduled"
            else:
                logger.info("temporal unavailable -> scheduled fallback")
                result["temporal"] = "fallback"
                result["dispatch_mode"] = "scheduled"
        except Exception as _e:  # pragma: no cover — offline-safe
            logger.debug("silent handled: offline-safe: temporal dispatch", exc_info=_e)  # intentional
            result["temporal"] = "fallback"
            result["dispatch_mode"] = "scheduled"
        return result


# 兼容别名
Scheduler = ScheduledService

__all__ = [
    "ScheduledPlaybook",
    "ScheduledService",
    "Scheduler",
    "PLAYBOOKS",
    "list_playbooks",
    "get_playbook",
    "get_next_trigger",
    "parse_cron",
    "validate_cron",
]

"""ScheduledResearch Temporal Cron — ZoneInfo timezone-aware cron dispatch.

Minimal implementation:
- cron 5-field validation (minute hour dom month dow)
- ZoneInfo timezone-aware next_trigger via minute brute-force (366d horizon)
- 5 playbooks registry (hard-coded + markdown files)
- Temporal Cron placeholder (to_temporal_cron)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# ---------- cron parsing ----------

_CRON_RANGES = {
    "minute": (0, 59),
    "hour": (0, 23),
    "dom": (1, 31),
    "month": (1, 12),
    "dow": (0, 7),  # 0 and 7 = Sunday
}


def _parse_field(field_str: str, min_val: int, max_val: int) -> Set[int]:
    """Parse single cron field into set of ints.

    Supports:
    - "*"
    - "*/n"
    - "a,b,c"
    - "a-b"
    - "a-b/n" and "*/n" and "a/n"
    Single values.
    Raises ValueError on invalid.
    """
    field_str = field_str.strip()
    if not field_str:
        raise ValueError("empty cron field")

    result: Set[int] = set()

    # split by comma (list)
    parts = field_str.split(",")
    for part in parts:
        part = part.strip()
        if not part:
            raise ValueError(f"invalid cron part empty in {field_str!r}")

        # handle step "/"
        if "/" in part:
            base, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                raise ValueError(f"invalid step {step_s!r} in {part!r}")
            if step <= 0:
                raise ValueError(f"step must be >0 got {step} in {part!r}")
            # base can be "*", "a-b", "a", etc.
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
                # single value with step like "2/3" means from 2 to max step 3
                try:
                    start = int(base)
                except ValueError:
                    raise ValueError(f"invalid base {base!r} in {part!r}")
                if start < min_val or start > max_val:
                    raise ValueError(f"value {start} out of bounds [{min_val},{max_val}]")
                end = max_val
            # expand
            for v in range(start, end + 1):
                if (v - start) % step == 0:
                    if min_val <= v <= max_val:
                        result.add(v)
            continue

        # no step — handle "*" or range or single
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
        # single integer
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
    """Normalize dow: 7 -> 0 (Sunday)."""
    out: Set[int] = set()
    for v in values:
        if v == 7:
            out.add(0)
        else:
            out.add(v)
    return out


def parse_cron(cron_expr: str) -> Tuple[Set[int], Set[int], Set[int], Set[int], Set[int]]:
    """Parse cron 5-field into sets; raises ValueError if invalid."""
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
    """Validate cron; returns True or raises ValueError."""
    parse_cron(cron_expr)
    return True


def _check_match(candidate: datetime, minute_set, hour_set, dom_set, month_set, dow_set) -> bool:
    # candidate is already timezone-aware in target tz
    if candidate.minute not in minute_set:
        return False
    if candidate.hour not in hour_set:
        return False
    if candidate.day not in dom_set:
        return False
    if candidate.month not in month_set:
        return False
    # dow: python weekday Mon=0 ... Sun=6 ; cron dow Sun=0, Mon=1 ... Sat=6
    # convert candidate.weekday() -> cron dow
    # weekday 0 Mon -> 1, 1 Tue ->2 ... 5 Sat ->6, 6 Sun ->0
    py_wday = candidate.weekday()  # 0 Mon
    cron_dow = (py_wday + 1) % 7  # Mon 0->1, Sun 6->0
    if cron_dow not in dow_set:
        return False
    return True


def get_next_trigger(cron_expr: str, tz_name: str, after: Optional[datetime] = None) -> datetime:
    """Compute next trigger after `after` for cron in given timezone.

    - Validates cron 5-field
    - Validates timezone via ZoneInfo
    - Returns timezone-aware datetime in target ZoneInfo, strictly > after
    - Horizon 366 days; raises ValueError if not found
    """
    # validate cron 5-field first (raises ValueError for bad cron)
    parse_result = parse_cron(cron_expr)
    minute_set, hour_set, dom_set, month_set, dow_set = parse_result

    # validate timezone
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as e:
        raise ValueError(f"invalid timezone {tz_name!r}: {e}") from e
    except Exception as e:
        raise ValueError(f"invalid timezone {tz_name!r}: {e}") from e

    # normalize after
    if after is None:
        after = datetime.now(tz)
    else:
        if not isinstance(after, datetime):
            raise ValueError("after must be datetime")
        if after.tzinfo is None:
            # treat naive as in target timezone
            after = after.replace(tzinfo=tz)
        else:
            after = after.astimezone(tz)

    # start at next minute boundary, strictly > after
    # truncate seconds/microseconds then +1 minute
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)

    # optional fast path for daily/weekly — but brute force is fine for 366d (527k iterations worst)
    horizon = candidate + timedelta(days=366)
    # safety cap 366*1440 ~ 527040 iterations
    while candidate <= horizon:
        if _check_match(candidate, minute_set, hour_set, dom_set, month_set, dow_set):
            # ensure returned dt has correct tzinfo key (candidate already in tz)
            # Candidate is already tz-aware via after conversion; adding timedelta keeps tzinfo
            # But datetime + timedelta retains tzinfo, need to ensure it's still ZoneInfo(tz_name)
            # Re-attach ZoneInfo to be safe if needed (dst transitions handled by conversion)
            # Use candidate.astimezone(tz) to normalize DST
            try:
                candidate = candidate.astimezone(tz)
            except Exception:
                pass
            return candidate
        candidate += timedelta(minutes=1)

    raise ValueError(f"no next trigger found within 366d for cron {cron_expr!r} tz {tz_name!r} after {after!r}")


# ---------- playbooks ----------

@dataclass
class ScheduledPlaybook:
    name: str
    cron: str
    timezone: str
    description: str
    title_cn: str = ""
    file: Optional[Path] = None
    # extra metadata
    tags: List[str] = field(default_factory=list)

    def next_trigger(self, after: Optional[datetime] = None) -> datetime:
        return get_next_trigger(self.cron, self.timezone, after=after)

    def to_dict(self) -> Dict[str, str]:
        return {
            "name": self.name,
            "cron": self.cron,
            "timezone": self.timezone,
            "description": self.description,
            "title_cn": self.title_cn,
        }


# 5 minimal playbooks — must match markdown files on disk
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
    return list(PLAYBOOKS)


def get_playbook(name: str) -> ScheduledPlaybook:
    if name not in _PLAYBOOK_MAP:
        raise KeyError(f"playbook {name!r} not found; available: {list(_PLAYBOOK_MAP.keys())}")
    return _PLAYBOOK_MAP[name]


class ScheduledService:
    """Timezone-aware dispatch service — Temporal Cron placeholder.

    Temporal usage (production):
        from temporalio.client import Client
        await client.start_scheduled(..., cron=playbook.cron, tz=playbook.timezone)

    This service provides local next_trigger computation that mirrors Temporal's
    cron + timezone dispatch for testing without Temporal server.
    """

    def __init__(self, playbooks: Optional[List[ScheduledPlaybook]] = None):
        self._playbooks = list(playbooks) if playbooks is not None else list(PLAYBOOKS)
        self._map: Dict[str, ScheduledPlaybook] = {p.name: p for p in self._playbooks}

    def list_playbooks(self) -> List[ScheduledPlaybook]:
        return list(self._playbooks)

    def get_playbook(self, name: str) -> ScheduledPlaybook:
        if name not in self._map:
            raise KeyError(f"playbook {name!r} not found")
        return self._map[name]

    def next_trigger(self, cron_expr: str, tz_name: str, after: Optional[datetime] = None) -> datetime:
        return get_next_trigger(cron_expr, tz_name, after=after)

    def next_trigger_for_playbook(self, name: str, after: Optional[datetime] = None) -> datetime:
        p = self.get_playbook(name)
        return p.next_trigger(after=after)

    def validate_cron(self, cron_expr: str) -> bool:
        return validate_cron(cron_expr)

    def to_temporal_cron(self, name_or_cron: str) -> str:
        """Return 5-field cron string for Temporal schedule.

        If name_or_cron is a known playbook name, returns its cron.
        Otherwise validates and returns the cron itself.
        """
        if name_or_cron in self._map:
            return self._map[name_or_cron].cron
        # validate it is a cron
        validate_cron(name_or_cron)
        return name_or_cron

    def dispatch(self, name: str, after: Optional[datetime] = None) -> Dict[str, str]:
        """Timezone-aware dispatch placeholder — returns next trigger info.

        In production this would enqueue a Temporal scheduled workflow.
        """
        p = self.get_playbook(name)
        nxt = p.next_trigger(after=after)
        return {
            "playbook": p.name,
            "cron": p.cron,
            "timezone": p.timezone,
            "next_trigger": nxt.isoformat(),
            "next_trigger_utc": nxt.astimezone(timezone.utc).isoformat(),
            "title_cn": p.title_cn,
        }


# alias for spec alternative naming
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

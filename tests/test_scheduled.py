"""Task15 ScheduledResearch Temporal Cron 5 playbooks — TDD red.

Assertions:
- cron 5-field validation + timezone ZoneInfo next_trigger correct
- 5 playbooks exist with cron/timezone and dispatch is timezone-aware
"""

import pytest
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path


def test_cron_five_field_timezone_next_trigger():
    """Cron 5-field + ZoneInfo must compute correct next_trigger."""
    from hero_quant.scheduled import get_next_trigger

    # Asia/Shanghai 08:30 weekdays — after 08:00 same day should be 08:30 same day
    after = datetime(2026, 8, 20, 8, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))  # Thu 2026-08-20 08:00 CST
    nxt = get_next_trigger("30 8 * * 1-5", "Asia/Shanghai", after=after)
    assert nxt is not None
    assert nxt.tzinfo is not None
    # timezone aware — ZoneInfo key
    assert getattr(nxt.tzinfo, "key", str(nxt.tzinfo)) == "Asia/Shanghai"
    assert nxt.hour == 8 and nxt.minute == 30
    assert nxt > after
    # weekday 1-5 means Thu -> should stay same day (Thu is 1-5)
    assert nxt.day == 20

    # After 09:00 same day should roll to next weekday (Fri 21)
    after2 = datetime(2026, 8, 20, 9, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    nxt2 = get_next_trigger("30 8 * * 1-5", "Asia/Shanghai", after=after2)
    assert nxt2.hour == 8 and nxt2.minute == 30
    assert nxt2.day == 21  # Friday

    # America/New_York 09:00 weekdays — DST aware
    after_ny = datetime(2026, 1, 5, 8, 0, 0, tzinfo=ZoneInfo("America/New_York"))  # Mon Jan 5 2026 08:00 EST
    nxt_ny = get_next_trigger("0 9 * * 1-5", "America/New_York", after=after_ny)
    assert nxt_ny.tzinfo is not None
    assert getattr(nxt_ny.tzinfo, "key", str(nxt_ny.tzinfo)) == "America/New_York"
    assert nxt_ny.hour == 9 and nxt_ny.minute == 0
    assert nxt_ny.day == 5

    # Weekend roll: Saturday should roll to Monday
    sat = datetime(2026, 8, 22, 10, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))  # Sat
    nxt_sat = get_next_trigger("30 8 * * 1-5", "Asia/Shanghai", after=sat)
    assert nxt_sat.weekday() < 5  # Mon-Fri
    assert nxt_sat.day == 24  # Monday 2026-08-24
    assert nxt_sat.hour == 8 and nxt_sat.minute == 30


def test_cron_invalid_raises():
    """Invalid cron (not 5-field or out-of-range) must raise ValueError."""
    from hero_quant.scheduled import get_next_trigger

    after = datetime(2026, 8, 20, 8, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    # not 5 fields
    with pytest.raises(ValueError):
        get_next_trigger("30 8 * *", "Asia/Shanghai", after=after)
    # out of range minute
    with pytest.raises(ValueError):
        get_next_trigger("60 8 * * *", "Asia/Shanghai", after=after)
    # out of range hour
    with pytest.raises(ValueError):
        get_next_trigger("30 24 * * *", "Asia/Shanghai", after=after)
    # invalid timezone
    with pytest.raises(Exception):
        get_next_trigger("30 8 * * *", "Invalid/Timezone", after=after)


def test_five_playbooks_exist_and_have_cron_timezone():
    """5 minimal playbooks must exist with cron 5-field + timezone + ZoneInfo dispatch."""
    from hero_quant.scheduled import list_playbooks, get_playbook, PLAYBOOKS

    # at least 5 playbooks
    assert len(PLAYBOOKS) >= 5, f"expected >=5 playbooks, got {len(PLAYBOOKS)}"
    playbooks = list_playbooks()
    assert len(playbooks) >= 5

    # expected 5 names
    names = {p.name for p in playbooks}
    expected = {"premarket-brief", "portfolio-checkup", "a-share-money-flow", "earnings-season-tracker", "institutional-holdings-diff"}
    assert expected.issubset(names), f"missing playbooks, got {names} expected {expected}"

    # each playbook must have valid cron 5-field + valid ZoneInfo timezone + next_trigger works
    for p in playbooks:
        assert hasattr(p, "cron") and hasattr(p, "timezone")
        # cron must be 5-field
        assert isinstance(p.cron, str) and len(p.cron.split()) == 5, f"{p.name} cron invalid: {p.cron!r}"
        # timezone must be valid ZoneInfo
        zi = ZoneInfo(p.timezone)  # should not raise
        assert zi is not None
        # next_trigger dispatch must be timezone-aware
        after = datetime(2026, 8, 20, 0, 0, 0, tzinfo=ZoneInfo(p.timezone))
        nxt = p.next_trigger(after=after)
        assert nxt.tzinfo is not None
        assert getattr(nxt.tzinfo, "key", str(nxt.tzinfo)) == p.timezone
        # also via service helper
        nxt2 = get_playbook(p.name).next_trigger(after=after)
        assert nxt2 == nxt


def test_playbook_markdown_files_exist():
    """Playbook markdown files must exist on disk (5 md files)."""
    base = Path("src/hero_quant/scheduled/playbooks")
    assert base.exists() and base.is_dir(), f"playbooks dir missing: {base}"
    mds = list(base.glob("*.md"))
    assert len(mds) >= 5, f"expected >=5 md files, got {len(mds)}: {mds}"
    names = {f.stem for f in mds}
    expected = {"premarket-brief", "portfolio-checkup", "a-share-money-flow", "earnings-season-tracker", "institutional-holdings-diff"}
    assert expected.issubset(names), f"missing md files, got {names}"


def test_scheduled_service_dispatch_timezone_aware():
    """ScheduledService dispatch must be timezone-aware (Temporal Cron placeholder)."""
    from hero_quant.scheduled import ScheduledService

    svc = ScheduledService()
    assert hasattr(svc, "next_trigger")
    assert hasattr(svc, "list_playbooks")
    # dispatch timezone difference: same cron different timezone gives different UTC instant
    after_utc = datetime(2026, 8, 20, 0, 0, 0, tzinfo=ZoneInfo("UTC"))
    nxt_sh = svc.next_trigger("30 8 * * *", "Asia/Shanghai", after=after_utc)
    nxt_ny = svc.next_trigger("30 8 * * *", "America/New_York", after=after_utc)
    assert nxt_sh.tzinfo is not None and nxt_ny.tzinfo is not None
    # same wall time 08:30 but different timezone => different UTC
    assert nxt_sh.astimezone(ZoneInfo("UTC")) != nxt_ny.astimezone(ZoneInfo("UTC"))
    # Temporal cron string helper if present
    if hasattr(svc, "to_temporal_cron"):
        tc = svc.to_temporal_cron("premarket-brief")
        assert isinstance(tc, str) and len(tc.split()) == 5

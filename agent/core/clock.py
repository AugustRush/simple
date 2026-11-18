"""What time it is, in the zone the person is actually in.

Why this is not one `strftime` in the turn loop
-----------------------------------------------
The block used to read ``Current UTC time: 2026-11-18 12:16 UTC``. Two defects,
both measured against a person in Asia/Shanghai:

1. **Eight hours a day, the date is wrong.** Beijing 00:00-07:59 is the previous
   UTC day. A model told "today is 2026-11-18" at 07:00 Beijing time answers
   about the wrong day -- and it is asked about days constantly: "明天提醒我",
   "下周三的周报", "这个月的账单".
2. **No weekday.** "下周三" cannot be resolved from a date without knowing what
   day today is, and a model computing it from a date is guessing. Every
   scheduling request the person makes in words goes through that guess.

So the line carries the local date, the weekday, the zone and the offset, and
the UTC time after it for correlation with logs. The weekday is spelled out
rather than numbered because "星期三" is what the person says.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

__all__ = ["clock_line", "WEEKDAYS_ZH"]

WEEKDAYS_ZH = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def clock_line(now: Optional[datetime] = None, *, timezone_name: str = "") -> str:
    """The turn's clock block: local first, UTC second.

    ``timezone_name`` is resolved at call time from the machine rather than
    captured at import: a long-running gateway should follow the host's zone if
    it changes, and a test needs to pin one.
    """
    from agent.scheduler.models import local_timezone_name

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    zone_name = timezone_name or local_timezone_name()
    try:
        from zoneinfo import ZoneInfo

        local = moment.astimezone(ZoneInfo(zone_name))
    except Exception:
        # An unreadable zone is not a reason to lose the clock: fall back to the
        # process's own offset, which is right for the host even when the IANA
        # name cannot be resolved.
        local = moment.astimezone()
        zone_name = str(local.tzname() or "local")
    offset = local.utcoffset() or timedelta(0)
    sign = "+" if offset.total_seconds() >= 0 else "-"
    hours, minutes = divmod(abs(int(offset.total_seconds())), 3600)
    offset_text = f"UTC{sign}{hours}" + (f":{minutes // 60:02d}" if minutes else "")
    weekday = WEEKDAYS_ZH[local.weekday()]
    # Terse on purpose: this rides in every turn's message, and the *habit* it
    # supports ("resolve 今天/下周三 from the local clock") belongs in the static
    # prompt where the other habits live, not repeated here 500 times a day.
    return (
        f"Current time: {local:%Y-%m-%d} {weekday} {local:%H:%M} "
        f"({zone_name}, {offset_text}) | UTC {moment.astimezone(timezone.utc):%Y-%m-%d %H:%M}"
    )

"""The clock the model is given, in the zone the person is in.

The block used to be ``Current UTC time: … UTC``. Two defects, both real for a
person in Asia/Shanghai:

* **eight hours a day the date is wrong** -- Beijing 00:00-07:59 is the previous
  UTC day, and the model is asked about days constantly;
* **no weekday**, so "下周三" has to be computed from a date, which is a guess.

These tests pin the *rendered line*, because the line is what the model reads.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.core.clock import WEEKDAYS_ZH, clock_line


def test_the_local_date_wins_over_the_utc_date_across_midnight() -> None:
    """The measured failure: 07:00 in Shanghai is the previous day in UTC."""
    line = clock_line(
        datetime(2026, 11, 18, 23, 0, tzinfo=timezone.utc),
        timezone_name="Asia/Shanghai",
    )

    assert "2026-11-19" in line, "the local date must be the one the person is living in"
    assert "星期四" in line
    assert "07:00" in line
    # UTC is still stated, because logs and run records are in it.
    assert "2026-11-18 23:00" in line


def test_the_weekday_is_spelled_out() -> None:
    """`下周三` cannot be resolved from a date without knowing today's weekday."""
    line = clock_line(
        datetime(2026, 11, 20, 4, 0, tzinfo=timezone.utc),
        timezone_name="Asia/Shanghai",
    )

    assert "星期五" in line
    assert all(day in WEEKDAYS_ZH for day in WEEKDAYS_ZH)


def test_the_zone_and_offset_are_named() -> None:
    """A bare time invites the model to assume its own zone."""
    line = clock_line(
        datetime(2026, 6, 1, 4, 0, tzinfo=timezone.utc),
        timezone_name="Asia/Shanghai",
    )

    assert "Asia/Shanghai" in line
    assert "UTC+8" in line


def test_the_line_stays_terse_because_it_rides_in_every_turn() -> None:
    """Facts here, habits in the static prompt.

    The first version put a full sentence of instructions in this line and it
    grew to ~430 characters. Measured: that pushed a turn's message past a
    small-but-valid input budget, which is a real failure mode for a block that
    is appended to every single turn.
    """
    line = clock_line(datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc))

    assert len(line) < 140, line
    assert "| UTC" in line, "UTC stays, for correlating with run records and logs"


def test_the_habit_lives_in_the_static_prompt() -> None:
    """The half that stops the wrong arithmetic, where it costs nothing per turn.

    Asserted on the *rendered* prompt, not the source: the sentence is split
    across string literals, and a source scan for a contiguous phrase is the kind
    of guard that passes while the rendered text says something else.
    """
    from pathlib import Path

    from agent import config as config_module

    class _Registry:
        _prompt_generation = 1

        def __init__(self) -> None:
            self._tools: dict = {}

        def get_context(self, _key):
            return None

    inputs = config_module._static_prompt_inputs(
        "base", _Registry(), Path("/ws"), Path("/out"), None
    )
    prompt = config_module._render_static_prompt(inputs)

    assert "Resolve 今天 / 明天 / 下周三" in prompt
    assert "previous day for eight hours" in prompt
    assert "current_time" in prompt


def test_an_unreadable_zone_still_produces_a_clock() -> None:
    """Losing the clock because a zone name is wrong would be worse than a guess."""
    line = clock_line(
        datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc),
        timezone_name="Not/AZone",
    )

    assert "2026-11-18" in line
    assert any(day in line for day in WEEKDAYS_ZH)


def test_a_naive_moment_is_read_as_utc_not_as_local() -> None:
    """The caller's naive datetime is UTC, which is what `_now()` produces."""
    naive = clock_line(datetime(2026, 11, 18, 23, 0), timezone_name="Asia/Shanghai")
    aware = clock_line(
        datetime(2026, 11, 18, 23, 0, tzinfo=timezone.utc),
        timezone_name="Asia/Shanghai",
    )

    assert naive == aware

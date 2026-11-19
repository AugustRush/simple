"""A turn that stopped mid-task, named back at the moment the model can act.

The gap: `set_goal` depended entirely on a line in the static prompt. The model
was told the tool exists and when it is appropriate, and then had to notice that
*this* turn was one of those moments. It never did.

The trigger is a fact this code can read; the decision stays with the model. An
automatically written goal would make "它给自己定的目标" false on the board, and
that board is where a person goes to find out what the agent is up to.
"""

from __future__ import annotations

import pytest

from agent.core.resume import resume_nudge


class _Event:
    def __init__(self, event_type: str, payload: dict, created_at: str = "") -> None:
        self.event_type = event_type
        self.payload = payload
        self.created_at = created_at


class _Store:
    def __init__(self, events: list[_Event]) -> None:
        self._events = events

    def recent_agent_events(self, *, session_id: str, limit: int = 20):
        return self._events


class _Goals:
    def __init__(self, goals: list[str], created_at: str = "") -> None:
        self._goals = goals
        self._created_at = created_at

    def list_goals(self, status: str = "open"):
        return [{"goal": item, "created_at": self._created_at} for item in self._goals]


def _nudge(events, goals=(), *, created_at="", goal_created_at=""):
    return resume_nudge(
        store=_Store(events),
        goal_store=_Goals(list(goals), goal_created_at),
        session_id="s1",
    )


def _finished(error: str = "", user: str = "归档旧截图", created_at: str = "") -> _Event:
    return _Event(
        "turn_finished", {"error": error, "user_content": user}, created_at=created_at
    )


def test_a_cancelled_turn_is_named_back_to_the_model() -> None:
    block = _nudge([_Event("turn_started", {}), _finished("Turn cancelled by user")])

    assert "上一轮没有做完" in block
    assert "归档旧截图" in block
    assert "set_goal" in block, "the block must name the tool it is asking for"
    assert "Turn cancelled by user" in block
    # Measured: without this, the model answered a *new* question by resuming the
    # old task ("我来做。先建目录和 30 个文件"), dropping what was just asked.
    assert "先回答我当前这条消息" in block
    assert "不要因为看到这段就丢下我现在说的话" in block
    # Default-on: measured over three cancellations, "if this should continue"
    # recorded nothing and never said why. Skipping has to be visible.
    assert "**默认**该用 `set_goal`" in block
    assert "并在回复里说一句" in block


def test_a_finished_turn_says_nothing() -> None:
    assert _nudge([_finished("", "写周报")]) == ""


def test_a_transient_error_is_not_work_left_undone() -> None:
    """A provider 429 is an error the person saw and can retry.

    Treating every rate limit as a half-finished task would mint self-set goals
    out of ordinary network weather.
    """
    assert _nudge([_finished("HTTP 429 rate limited", "查资料")]) == ""


def test_an_already_recorded_goal_silences_it() -> None:
    """Otherwise it repeats every turn after the goal was set -- the exact noise
    that teaches a reader to skip the block."""
    assert (
        _nudge(
            [_finished("cancelled_by_user", "归档旧截图", "2026-11-19T12:00:00+00:00")],
            goals=["把旧截图归档"],
            goal_created_at="2026-11-19T12:05:00+00:00",
        )
        == ""
    )


def test_a_goal_written_before_the_interruption_does_not_silence_it() -> None:
    block = _nudge(
        [_finished("cancelled_by_user", "归档旧截图", "2026-11-19T12:00:00+00:00")],
        goals=["每周五写周报"],
        goal_created_at="2026-11-01T09:00:00+00:00",
    )
    assert "归档旧截图" in block


def test_it_is_one_shot_by_construction() -> None:
    """The turn that answers the nudge writes its own clean `turn_finished`, so
    the next call sees a finished turn. No counter to forget to clear."""
    events = [_finished("cancelled_by_user", "归档旧截图")]
    assert _nudge(events) != ""

    events.append(_finished("", "另一个问题"))
    assert _nudge(events) == ""


def test_no_events_is_silent_not_an_error() -> None:
    assert _nudge([]) == ""


def test_a_missing_store_is_silent() -> None:
    assert resume_nudge(store=None, goal_store=_Goals([]), session_id="") == ""


def test_it_reaches_the_rendered_turn() -> None:
    """The block is worth nothing if it is computed and never appended."""
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[1] / "agent" / "core" / "turn_loop.py"
    ).read_text(encoding="utf-8")

    assert "resume_nudge(" in source
    assert "turn_blocks.append(nudge)" in source

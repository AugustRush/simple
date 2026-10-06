"""Ending a turn whose tool loop stopped making progress.

The watchdog already decided *whether* the turn was stuck.  What moved out of
``send_message`` is what to do about it: five effects for one decision, ending the
turn from inside a ``try`` whose ``finally`` resets three contextvars.

Two things are easy to get wrong here and both are asserted:

- the response is appended to the conversation *and* returned, because the turn
  ends immediately -- a reply the user saw but the conversation does not hold is
  lost on replay;
- the early return became a field on a decision object, because a ``return``
  inside an extracted function exits the helper and would skip that ``finally``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.core.tool_loop_decision import StuckDecision, handle_stuck_loop


class _Ctx:
    agent_id = "a1"

    def __init__(self) -> None:
        self.messages: list = [{"role": "user", "content": "go"}]


class _Agent:
    def __init__(self, reason="") -> None:
        self.reason = reason
        self.checked: list = []
        self.built: list = []

    def _check_tool_loop_stuck(self, watchdog, tool_uses, results):
        self.checked.append((watchdog, len(tool_uses), len(results)))
        return self.reason

    def _build_tool_loop_stuck_response(self, **kwargs):
        self.built.append(kwargs)
        return f"stuck because {kwargs['reason']}"


def _run(agent, *, ctx=None, log=None, calls=None, **overrides):
    kwargs = dict(
        agent=agent,
        ctx=ctx if ctx is not None else _Ctx(),
        watchdog={"last_outcome": {"status": "unproductive"}},
        tool_uses=[{"name": "read_file"}],
        results=["R"],
        user_message="the task",
        tool_calls_made=["read_file"],
        step_index=2,
        trace_status="ok",
        interaction_log=log,
    )
    kwargs.update(overrides)
    return handle_stuck_loop(**kwargs)


# ── The ordinary case ────────────────────────────────────────────────────────


def test_a_healthy_loop_reports_not_stuck() -> None:
    """This runs on every tool step and almost never fires."""
    decision = _run(_Agent(reason=""))
    assert decision.stuck is False
    assert decision.final_result is None


def test_a_healthy_loop_changes_nothing() -> None:
    ctx = _Ctx()
    before = list(ctx.messages)
    agent = _Agent(reason="")
    _run(agent, ctx=ctx)
    assert ctx.messages == before, "no response for a turn that is not stuck"
    assert agent.built == [], "no need to build one"


def test_a_healthy_loop_reports_no_reason() -> None:
    assert _run(_Agent(reason="")).reason == ""


def test_the_watchdog_is_consulted_with_the_batch() -> None:
    agent = _Agent(reason="")
    _run(agent, tool_uses=[{"name": "a"}, {"name": "b"}], results=["x", "y"])
    assert agent.checked[0][1] == 2
    assert agent.checked[0][2] == 2


# ── The stuck case ───────────────────────────────────────────────────────────


def test_a_stuck_loop_ends_the_turn() -> None:
    decision = _run(_Agent(reason="repeating the same call"))
    assert decision.stuck is True
    assert decision.final_result is not None
    assert decision.final_result.content == "stuck because repeating the same call"


def test_the_response_is_appended_to_the_conversation() -> None:
    """The turn ends here, so the reply must also be in the history."""
    ctx = _Ctx()
    _run(_Agent(reason="r"), ctx=ctx)
    assert ctx.messages[-1] == {
        "role": "assistant",
        "content": "stuck because r",
    }


def test_the_appended_text_is_the_reported_text() -> None:
    ctx = _Ctx()
    decision = _run(_Agent(reason="r"), ctx=ctx)
    assert ctx.messages[-1]["content"] == decision.text
    assert decision.final_result.content == decision.text


def test_the_reason_is_reported_on_the_decision() -> None:
    """The caller sets ``trace_status`` from the fact, not from re-checking."""
    assert _run(_Agent(reason="looping")).reason == "looping"


def test_the_result_carries_the_tool_calls_made() -> None:
    decision = _run(_Agent(reason="r"), tool_calls_made=["a", "b"])
    assert decision.final_result.tool_calls_made == ["a", "b"]


def test_the_result_carries_the_agent_id() -> None:
    ctx = _Ctx()
    decision = _run(_Agent(reason="r"), ctx=ctx)
    assert decision.final_result.agent_id == ctx.agent_id


def test_the_response_builder_receives_the_batch() -> None:
    """It explains which calls repeated, so it needs the calls and the results."""
    agent = _Agent(reason="r")
    _run(
        agent,
        tool_uses=[{"name": "read_file"}],
        results=["R"],
        user_message="the task",
    )
    built = agent.built[0]
    assert built["reason"] == "r"
    assert built["tool_uses"] == [{"name": "read_file"}]
    assert built["results"] == ["R"]
    assert built["user_message"] == "the task"


# ── The log line ─────────────────────────────────────────────────────────────


def test_the_stop_is_logged_with_the_watchdog_verdict() -> None:
    """Without the outcome, a stuck report says what but not why."""
    events: list = []
    _run(
        _Agent(reason="looping"),
        log=lambda name, **fields: events.append((name, fields)),
    )
    name, fields = events[0]
    assert name == "tool_loop_watchdog_stopped"
    assert fields["reason"] == "looping"
    assert fields["tool_outcome"] == {"status": "unproductive"}


def test_the_log_names_the_tools_that_repeated() -> None:
    """Capped at five: the line explains a loop, it is not a transcript."""
    events: list = []
    _run(
        _Agent(reason="r"),
        tool_uses=[{"name": f"t{i}"} for i in range(8)],
        results=["x"] * 8,
        log=lambda name, **fields: events.append(fields),
    )
    assert events[0]["tool_names"] == "t0,t1,t2,t3,t4"
    assert events[0]["tool_uses"] == 8


def test_the_log_reports_the_one_based_step() -> None:
    events: list = []
    _run(
        _Agent(reason="r"),
        step_index=2,
        log=lambda name, **fields: events.append(fields),
    )
    assert events[0]["step"] == 3, "the user-facing step number"


def test_a_missing_watchdog_outcome_is_tolerated() -> None:
    """A watchdog dict without a recorded verdict still reports the stop."""
    events: list = []
    _run(
        _Agent(reason="r"),
        watchdog={},
        log=lambda name, **fields: events.append(fields),
    )
    assert events[0]["tool_outcome"] == {}


def test_logging_is_optional() -> None:
    decision = _run(_Agent(reason="r"), log=None)
    assert decision.stuck is True


def test_the_stop_is_not_logged_when_nothing_is_stuck() -> None:
    events: list = []
    _run(
        _Agent(reason=""),
        log=lambda name, **fields: events.append(name),
    )
    assert events == []


def test_a_decision_is_frozen() -> None:
    with pytest.raises(Exception):
        StuckDecision().stuck = True  # type: ignore[misc]

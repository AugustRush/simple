"""A method defined twice is a method whose first body is dead.

``BaseAgent`` defined ``_new_watchdog_state`` and ``_check_tool_loop_stuck``
twice: the original inline bodies first, then delegating versions after the
extraction to :mod:`agent.core.tool_watchdog`.  Python keeps the last definition,
so 64 lines -- including a 53-line implementation that looked canonical -- never
ran.  The interpreter accepts this silently; only reading the class would reveal
it.

The guard below is the point of this file.  The behavioural tests record what the
delegates are supposed to do, so that removing the dead copies is not just a
deletion but a deletion whose surviving behaviour is written down.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent.core.tool_watchdog import ToolLoopWatchdog

AGENT_PY = Path(__file__).resolve().parents[1] / "agent/core/agent.py"


def _methods() -> dict[str, int]:
    tree = ast.parse(AGENT_PY.read_text(encoding="utf-8"))
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "BaseAgent"
    )
    counts: dict[str, int] = {}
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            counts[node.name] = counts.get(node.name, 0) + 1
    return counts


# ── The guard ────────────────────────────────────────────────────────────────


def test_no_method_is_defined_more_than_once() -> None:
    """A duplicate silently shadows its predecessor, with no runtime signal.

    Checked at the source because there is no probe for "an earlier definition of
    this name is unreachable" -- the class simply has one fewer method than it
    appears to, and the dead body keeps looking authoritative.
    """
    counts = _methods()
    duplicates = {name: count for name, count in counts.items() if count > 1}
    assert not duplicates, (
        "defined more than once, so all but the last body is dead:\n  "
        + "\n  ".join(f"{name} x{count}" for name, count in sorted(duplicates.items()))
    )


def test_the_watchdog_methods_exist_once_each() -> None:
    """The specific pair that was duplicated, named so it cannot come back quietly."""
    counts = _methods()
    for name in ("_new_watchdog_state", "_check_tool_loop_stuck"):
        assert counts.get(name) == 1, f"{name} must have exactly one definition"


def test_the_live_methods_delegate_to_the_module() -> None:
    """They are the short delegates, not a second inline implementation."""
    import inspect

    import agent as agent_module

    assert "ToolLoopWatchdog.new_state()" in inspect.getsource(
        agent_module.BaseAgent._new_watchdog_state
    )
    assert "self._tool_watchdog.check" in inspect.getsource(
        agent_module.BaseAgent._check_tool_loop_stuck
    )


# ── The surviving behaviour ──────────────────────────────────────────────────

_FAILED = '{"ok": false, "error": "boom"}'
_SUCCESS = '{"ok": true, "content": "fine"}'


def _watchdog() -> ToolLoopWatchdog:
    return ToolLoopWatchdog(repeat_threshold=2, unproductive_threshold=2)


def test_a_fresh_state_has_every_counter() -> None:
    """The initial shape the removed inline literal spelled out."""
    state = ToolLoopWatchdog.new_state()
    assert set(state) == {
        "last_pair_sig",
        "last_result_sig",
        "same_pair",
        "same_result",
        "unproductive_rounds",
        "batch_counts",
        "last_outcome",
    }


def test_a_fresh_state_starts_empty() -> None:
    state = ToolLoopWatchdog.new_state()
    for key in ("last_pair_sig", "last_result_sig"):
        assert state[key] == ""
    for key in ("same_pair", "same_result", "unproductive_rounds"):
        assert state[key] == 0
    assert state["batch_counts"] == {}
    assert state["last_outcome"] == {}


def test_a_productive_batch_reports_nothing() -> None:
    assert _watchdog().check(
        ToolLoopWatchdog.new_state(), [{"name": "t"}], [_SUCCESS]
    ) == ""


def test_repeating_the_same_pair_is_reported() -> None:
    """The primary rule: the same calls returning the same thing."""
    watchdog = _watchdog()
    state = ToolLoopWatchdog.new_state()
    reason = ""
    for _ in range(3):
        reason = watchdog.check(state, [{"name": "read_file"}], [_FAILED])
    assert reason == "same_pair"


def test_the_run_length_counters_advance_per_call() -> None:
    watchdog = _watchdog()
    state = ToolLoopWatchdog.new_state()
    watchdog.check(state, [{"name": "t"}], [_FAILED])
    first_pair = state["same_pair"]
    watchdog.check(state, [{"name": "t"}], [_FAILED])
    assert state["same_pair"] == first_pair + 1


def test_the_outcome_is_recorded_for_the_log_line() -> None:
    """The watchdog verdict that the stuck-response log reports."""
    state = ToolLoopWatchdog.new_state()
    _watchdog().check(state, [{"name": "t"}], [_FAILED])
    assert state["last_outcome"], "the caller reads this to explain the stop"


def test_unproductive_rounds_advance_on_the_same_call_that_returns() -> None:
    """The ordering the dead inline body encoded, asserted where it now lives.

    The counters are updated *before* the rules are tested, so a batch that both
    is unproductive and trips a rule advances ``unproductive_rounds`` in the same
    call.  Moving the update after the early returns would make the threshold fire
    one step later, and no test would see it -- because the code that would have
    to change was the dead copy.
    """
    watchdog = _watchdog()
    state = ToolLoopWatchdog.new_state()
    reason = ""
    for _ in range(3):
        reason = watchdog.check(state, [{"name": "t"}], [_FAILED])
    assert state["unproductive_rounds"] >= 1, "advanced while a rule was firing"
    assert reason, "and a rule did fire"


def test_a_mixed_batch_is_not_treated_as_unproductive() -> None:
    """One success is enough -- the asymmetry the classifier documents."""
    watchdog = _watchdog()
    state = ToolLoopWatchdog.new_state()
    assert watchdog.check(state, [{"name": "t"}], [_FAILED, _SUCCESS]) == ""


def test_the_batch_threshold_is_separable() -> None:
    """It defaults to the unproductive threshold but can be set apart."""
    default = ToolLoopWatchdog(repeat_threshold=2, unproductive_threshold=3)
    separate = ToolLoopWatchdog(
        repeat_threshold=2, unproductive_threshold=3, batch_threshold=2
    )
    assert default._batch_threshold == 3
    assert separate._batch_threshold == 2


def test_the_dead_copy_is_gone_from_the_source() -> None:
    """The 53-line inline implementation must not reappear as a second body."""
    source = AGENT_PY.read_text(encoding="utf-8")
    assert "state['batch_counts'][batch_sig] = state['batch_counts'].get(" not in source, (
        "the inline watchdog body is back -- it would shadow, or be shadowed by, "
        "the extracted module"
    )

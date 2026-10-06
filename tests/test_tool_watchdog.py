"""Tool-loop stuck detection, drivable without an agent.

Thirteen methods and three constants on ``BaseAgent`` answered one question: has
this loop stopped getting anywhere?  The state machine lived inside a 4700-line
class, so testing it meant building an agent and pushing tool results through
the real turn loop.

``ToolLoopWatchdog`` takes its two thresholds and owns the rest, so the rules
can be driven directly.  The tests below cover each rule, the counters behind
it, and the priority order between them.
"""

from __future__ import annotations

import json

import pytest

from agent.core.tool_watchdog import ToolLoopOutcome, ToolLoopWatchdog


def _watchdog(repeat: int = 3, unproductive: int = 4) -> ToolLoopWatchdog:
    return ToolLoopWatchdog(
        repeat_threshold=repeat, unproductive_threshold=unproductive
    )


def _ok(**kwargs) -> str:
    return json.dumps({"ok": True, **kwargs})


def _fail(reason: str = "boom") -> str:
    return json.dumps({"ok": False, "error": reason})


# ── Signatures are stable across noise ───────────────────────────────────────


def test_the_same_call_with_reordered_keys_has_one_signature() -> None:
    """A provider that reorders an object did not make a different call."""
    a = [{"name": "read_file", "input": {"path": "x", "limit": 1}}]
    b = [{"name": "read_file", "input": {"limit": 1, "path": "x"}}]
    assert ToolLoopWatchdog.tool_use_signature(a) == ToolLoopWatchdog.tool_use_signature(b)


def test_volatile_result_keys_are_ignored() -> None:
    """A fresh operation id must not make a repeat look like progress."""
    first = _ok(operation_id="a", elapsed_ms=1, content="same")
    second = _ok(operation_id="b", elapsed_ms=999, content="same")
    assert ToolLoopWatchdog.tool_result_signature([first]) == (
        ToolLoopWatchdog.tool_result_signature([second])
    )


def test_a_meaningful_result_change_changes_the_signature() -> None:
    """Control: the filter must not collapse everything to one signature."""
    first = _ok(content="same")
    second = _ok(content="different")
    assert ToolLoopWatchdog.tool_result_signature([first]) != (
        ToolLoopWatchdog.tool_result_signature([second])
    )


# ── Classification ───────────────────────────────────────────────────────────


def test_an_empty_result_is_unproductive() -> None:
    assert ToolLoopWatchdog.classify_tool_result("").unproductive is True


def test_a_successful_payload_with_content_is_productive() -> None:
    assert ToolLoopWatchdog.classify_tool_result(_ok(content="x")).unproductive is False


def test_a_recoverable_failure_is_retryable_not_fatal() -> None:
    """The distinction that keeps the loop retrying things that can succeed."""
    outcome = ToolLoopWatchdog.classify_tool_result(
        json.dumps({"ok": False, "recoverable_by_agent": True})
    )
    assert outcome.retryable is True
    assert outcome.unproductive is False
    assert outcome.fatal is False


def test_a_plain_failure_is_fatal_and_unproductive() -> None:
    outcome = ToolLoopWatchdog.classify_tool_result(_fail())
    assert outcome.fatal is True
    assert outcome.unproductive is True


def test_an_intent_requirement_needs_the_user() -> None:
    outcome = ToolLoopWatchdog.classify_tool_result(
        json.dumps({"ok": False, "intent_required": True})
    )
    assert outcome.intent_required is True
    assert outcome.needs_user is True


def test_a_blocked_payload_is_unproductive() -> None:
    outcome = ToolLoopWatchdog.classify_tool_result(json.dumps({"blocked": True}))
    assert outcome.unproductive is True


def test_a_non_json_failure_marker_is_unproductive() -> None:
    assert ToolLoopWatchdog.classify_tool_result("Error: timed out").unproductive is True


def test_a_non_json_plain_result_is_productive() -> None:
    assert ToolLoopWatchdog.classify_tool_result("all good").unproductive is False


def test_batch_fatal_uses_all_when_unproductive_and_any_otherwise() -> None:
    """The asymmetry is real and worth pinning, because it reads backwards.

    A batch is ``unproductive`` only when *every* outcome was.  ``fatal`` is
    ``all(...)`` over the unproductive outcomes **when the batch is
    unproductive**, and ``any(...)`` otherwise -- so a batch containing one
    fatal failure and one recoverable failure is not unproductive, and its
    ``fatal`` comes from the ``any`` branch.
    """
    all_fatal = ToolLoopWatchdog.classify_tool_results([_fail(), _fail("again")])
    assert all_fatal.unproductive is True
    assert all_fatal.fatal is True
    assert all_fatal.retryable is False

    mixed = ToolLoopWatchdog.classify_tool_results(
        [_fail(), json.dumps({"ok": False, "recoverable_by_agent": True})]
    )
    assert mixed.unproductive is False, "the recoverable outcome was productive"
    assert mixed.retryable is True
    assert mixed.fatal is True, "any() over a batch that is not unproductive"

    recoverable_only = ToolLoopWatchdog.classify_tool_results(
        [json.dumps({"ok": False, "recoverable_by_agent": True})]
    )
    assert recoverable_only.fatal is False


# ── The rules, and their order ───────────────────────────────────────────────


def test_a_varying_loop_is_not_reported() -> None:
    """Control: productive, always-different rounds must never trip."""
    watchdog = _watchdog()
    state = watchdog.new_state()
    for step in range(10):
        reason = watchdog.check(
            state,
            [{"name": "read_file", "input": {"path": f"f{step}"}}],
            [_ok(content=f"c{step}")],
        )
        assert reason == ""


def test_the_same_pair_trips_first() -> None:
    watchdog = _watchdog(repeat=3)
    state = watchdog.new_state()
    call = [{"name": "shell", "input": {"command": "ls"}}]
    result = [_ok(stdout="same")]

    reasons = [watchdog.check(state, call, result) for _ in range(3)]
    assert reasons[:2] == ["", ""]
    assert reasons[2] == "same_pair"


def test_intent_required_outranks_same_pair() -> None:
    """The priority order: the one condition a retry cannot fix is reported.

    A repeated intent refusal also repeats the *pair*, so without the priority
    the user would be told "same_pair" and never learn that the fix is to state
    an intent.
    """
    watchdog = _watchdog(repeat=3)
    state = watchdog.new_state()
    call = [{"name": "write_file", "input": {"path": "x"}}]
    result = [json.dumps({"ok": False, "intent_required": True})]

    reasons = [watchdog.check(state, call, result) for _ in range(3)]
    assert reasons[2] == "intent_required"


def test_an_unproductive_batch_that_keeps_changing_trips_on_the_count() -> None:
    """Different calls, different failures: no pair repeat, still a loop."""
    watchdog = _watchdog(repeat=3, unproductive=4)
    state = watchdog.new_state()
    reasons = []
    for step in range(4):
        reasons.append(
            watchdog.check(
                state,
                [{"name": "read_file", "input": {"path": f"missing{step}"}}],
                [_fail(f"not found {step}")],
            )
        )
    assert reasons[-1] != ""


def test_a_new_batch_makes_progress_again() -> None:
    """The counters are run-lengths, so one productive round resets them."""
    watchdog = _watchdog(repeat=3)
    state = watchdog.new_state()
    call = [{"name": "shell", "input": {"command": "ls"}}]
    stuck = [_ok(stdout="same")]
    for _ in range(2):
        watchdog.check(state, call, stuck)
    assert state["same_pair"] == 2

    watchdog.check(state, call, [_ok(stdout="changed")])
    assert state["same_pair"] == 1
    assert state["unproductive_rounds"] == 0


def test_state_records_the_last_outcome() -> None:
    """The turn loop reads this for reporting; it must keep being written."""
    watchdog = _watchdog()
    state = watchdog.new_state()
    watchdog.check(state, [{"name": "t", "input": {}}], [_ok(content="x")])
    assert set(state["last_outcome"]) == set(ToolLoopOutcome().to_dict())


def test_the_same_batch_trips_on_its_occurrence_count() -> None:
    """Same call, *different* failures: the pair never repeats, the batch does."""
    watchdog = ToolLoopWatchdog(
        repeat_threshold=10, unproductive_threshold=99, batch_threshold=4
    )
    state = watchdog.new_state()
    call = [{"name": "read_file", "input": {"path": "flaky"}}]
    reasons = [
        watchdog.check(state, call, [_fail(f"attempt {step}")]) for step in range(4)
    ]
    assert reasons[:3] == ["", "", ""]
    assert reasons[3] == "same_batch_unproductive"


def test_the_occurrence_rule_is_unreachable_at_the_default_thresholds() -> None:
    """Measured, not assumed, and pinned so the docstring stays true.

    Both counters advance on the same unproductive rounds, but the run-length
    counter also counts rounds belonging to *other* batch signatures, so it can
    never be behind.  With one shared threshold the occurrence rule cannot fire.
    """
    watchdog = _watchdog(repeat=10, unproductive=4)
    state = watchdog.new_state()
    call = [{"name": "read_file", "input": {"path": "flaky"}}]
    reasons = [
        watchdog.check(state, call, [_fail(f"attempt {step}")]) for step in range(4)
    ]
    assert reasons[3] == "unproductive_rounds"
    assert "same_batch_unproductive" not in reasons


# ── The stop message ─────────────────────────────────────────────────────────


def test_the_stop_message_follows_the_user_s_language() -> None:
    """A stop the user has to act on should be in the language they wrote."""
    zh = ToolLoopWatchdog.build_stuck_response(
        user_message="帮我读一下这个文件",
        reason="same_pair",
        tool_uses=[{"name": "read_file"}],
        results=[_fail()],
    )
    en = ToolLoopWatchdog.build_stuck_response(
        user_message="please read this file",
        reason="same_pair",
        tool_uses=[{"name": "read_file"}],
        results=[_fail()],
    )
    assert "我先停一下" in zh
    assert "I am pausing" in en


def test_the_intent_message_explains_the_fix() -> None:
    """The one stop where the user can unblock it must say how."""
    message = ToolLoopWatchdog.build_stuck_response(
        user_message="write it",
        reason="intent_required",
        tool_uses=[{"name": "write_file"}],
        results=[json.dumps({"ok": False, "intent_required": True})],
    )
    assert "intent" in message
    assert "write_file" in message


def test_the_stop_message_names_the_tool_and_describes_the_rule() -> None:
    """The rule reaches the user as a sentence, not as the internal key.

    ``same_pair`` is rendered as "the same tool request produced the same
    result repeatedly" -- the reason string is a code identifier and quoting it
    at the user would be the leak of an implementation detail.
    """
    message = ToolLoopWatchdog.build_stuck_response(
        user_message="go",
        reason="same_pair",
        tool_uses=[{"name": "shell"}],
        results=[_ok(stdout="same")],
    )
    assert "shell" in message
    assert "the same tool request produced the same result repeatedly" in message
    assert "same_pair" not in message

    zh = ToolLoopWatchdog.build_stuck_response(
        user_message="继续",
        reason="unproductive_rounds",
        tool_uses=[{"name": "shell"}],
        results=[_fail()],
    )
    assert "连续多轮工具调用失败或没有有效输出" in zh


def test_the_preview_is_single_line_and_bounded() -> None:
    preview = ToolLoopWatchdog.result_preview(["a\nb", "c" * 500], limit=40)
    assert "\n" not in preview
    assert len(preview) <= 40


# ── The agent still delegates ────────────────────────────────────────────────


def test_the_agent_methods_agree_with_the_watchdog() -> None:
    """A wrapper that silently stops wrapping is the failure mode to catch."""
    import agent as agent_module

    agent = agent_module.BaseAgent(
        object(), agent_module.ToolRegistry(), model="fake-model", api_format="openai"
    )
    state = agent._new_watchdog_state()
    assert agent._check_tool_loop_stuck(state, [], []) == ""
    assert agent._classify_tool_result(_ok(content="x")).unproductive is False
    assert agent._tool_result_looks_unproductive(_fail()) is True
    assert agent._tool_use_signature([]) == ToolLoopWatchdog.tool_use_signature([])
    assert agent._stable_tool_loop_key({"a": 1}) == ToolLoopWatchdog.stable_key({"a": 1})
    assert agent._tool_result_preview(["x"]) == ToolLoopWatchdog.result_preview(["x"])

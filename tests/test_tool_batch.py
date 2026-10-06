"""Running one batch of tool calls and screening the results.

Extracted from ``send_message``'s tool-use branch, where it was 68 lines with one
decision at the end.  The tests below are written around the three *effects* that
had to survive the move, because one of them did not:

- ``tool_result_history`` gains a summary pair per tool call.  Dropping this
  statement was a real mistake during extraction, and its symptom was a turn that
  fell back to the generic "I received your message but..." reply instead of
  synthesizing an answer from the tool output.  Nothing failed loudly; a turn that
  had done its work simply answered worse.
- the assistant-text token is set and always reset, so a failed batch does not
  leak a stale value into the next step;
- a flagged result produces a ``restart_message`` rather than being submitted.
"""

from __future__ import annotations

import asyncio

import pytest

from agent.core.tool_batch import ToolBatchOutcome, run_tool_batch


class _Ctx:
    agent_id = "a1"
    metadata: dict = {}


class _Filter:
    def __init__(self, risky=()) -> None:
        self.risky = set(risky)
        self.learned: list = []

    async def learn_and_persist(self, results) -> None:
        self.learned.append(list(results))


class _Registry:
    def tool_capabilities(self, name):
        return {"orchestration"} if name.startswith("spawn") else {"read"}


class _Token:
    """Stands in for the contextvar the assistant text rides on."""

    def __init__(self) -> None:
        self.value = None
        self.sets: list = []
        self.resets: list = []

    def set(self, value):
        self.sets.append(value)
        previous, self.value = self.value, value
        return previous

    def reset(self, token) -> None:
        self.resets.append(token)
        self.value = token


class _Agent:
    def __init__(self, results=None, restart=None, risky=()) -> None:
        self.results = results if results is not None else ["RESULT-1"]
        self.restart = restart
        self.calls: list = []
        self.content_filter = _Filter(risky=risky)
        self._content_filter_threshold = 0.5
        self.registry = _Registry()

    async def _run_tool_uses(self, tool_uses, orchestration_decision=None):
        self.calls.append(("run", len(tool_uses)))
        return list(self.results)

    def _extract_summary_text(self, result):
        return f"summary({result})"

    def _clear_context_restart_message(self, tool_uses, results):
        return self.restart


def _run(agent, *, tool_uses=None, history=None, **overrides):
    tool_uses = tool_uses if tool_uses is not None else [{"name": "read_file"}]
    kwargs = dict(
        agent=agent,
        ctx=_Ctx(),
        tool_uses=tool_uses,
        orchestration_decision=None,
        intent_text="the intent",
        tool_result_history=history,
        step_index=0,
        watchdog=None,
        interaction_log=None,
        trace_latency=None,
        logger=None,
        filter_tool_results=lambda cf, tu, res, threshold: (list(res), []),
        active_assistant_text=None,
    )
    kwargs.update(overrides)
    return asyncio.run(run_tool_batch(**kwargs)), tool_uses


# ── The effect that was dropped ──────────────────────────────────────────────


def test_the_history_gains_one_summary_pair_per_tool_call() -> None:
    """The statement that went missing, and the reason the tests exist.

    Without it a tool-only turn has no history to synthesize from, so it answers
    with the generic fallback rather than the tool's own result -- a silent
    quality regression, not a failure.
    """
    history: list = []
    agent = _Agent(results=["R1", "R2"])
    _run(
        agent,
        tool_uses=[{"name": "read_file"}, {"name": "write_file"}],
        history=history,
    )
    assert history == [
        ("read_file", "summary(R1)"),
        ("write_file", "summary(R2)"),
    ]


def test_the_history_is_appended_to_not_replaced() -> None:
    """It is a running log across every step of the turn."""
    history = [("earlier", "summary(earlier)")]
    _run(_Agent(), history=history)
    assert history[0] == ("earlier", "summary(earlier)")
    assert len(history) == 2


def test_history_is_optional() -> None:
    """A caller with no turn-level log must still be able to run a batch."""
    outcome, _ = _run(_Agent(), history=None)
    assert outcome.results == ["RESULT-1"]


# ── The assistant-text token ─────────────────────────────────────────────────


def test_the_intent_text_is_published_to_tools() -> None:
    token = _Token()
    _run(_Agent(), active_assistant_text=token)
    assert token.sets == ["the intent"]


def test_the_token_is_reset_even_when_the_batch_raises() -> None:
    """A stale value would leak into the next step's tool calls."""
    token = _Token()

    class _Boom(_Agent):
        async def _run_tool_uses(self, tool_uses, orchestration_decision=None):
            raise RuntimeError("tool exploded")

    with pytest.raises(RuntimeError, match="tool exploded"):
        _run(_Boom(), active_assistant_text=token)
    assert len(token.resets) == 1, "the finally must run"


def test_no_token_means_no_set_or_reset() -> None:
    outcome, _ = _run(_Agent(), active_assistant_text=None)
    assert outcome.results


# ── The restart decision ─────────────────────────────────────────────────────


def test_a_restart_message_is_reported_not_applied() -> None:
    """The caller owns ``ctx.messages`` and the loop counter."""
    outcome, _ = _run(_Agent(restart="START OVER"))
    assert outcome.restart_message == "START OVER"


def test_no_restart_is_reported_as_none() -> None:
    outcome, _ = _run(_Agent())
    assert outcome.restart_message is None


def test_the_raw_results_come_back_for_the_caller() -> None:
    """They are needed afterwards: rollback, loop detection, remaining work."""
    outcome, _ = _run(_Agent(results=["A", "B"]))
    assert outcome.results == ["A", "B"]


def test_the_outcome_carries_the_filtered_results() -> None:
    agent = _Agent(results=["raw"])
    outcome, _ = _run(
        agent,
        filter_tool_results=lambda cf, tu, res, threshold: (["FILTERED"], [0]),
    )
    assert outcome.filtered_results == ["FILTERED"]
    assert outcome.risky_indices == [0]


# ── The content filter ───────────────────────────────────────────────────────


def test_flagged_results_are_learned_from_the_raw_output() -> None:
    """It must learn the real text, not the replacement it produced."""
    agent = _Agent(results=["raw-risky"])
    _run(
        agent,
        filter_tool_results=lambda cf, tu, res, threshold: (["SUMMARY"], [0]),
    )
    assert agent.content_filter.learned == [["raw-risky"]]


def test_nothing_is_learned_when_nothing_is_flagged() -> None:
    agent = _Agent()
    _run(agent, filter_tool_results=lambda cf, tu, res, threshold: (list(res), []))
    assert agent.content_filter.learned == []


def test_the_filter_receives_the_configured_threshold() -> None:
    seen: dict = {}

    def spy(content_filter, tool_uses, results, threshold):
        seen["threshold"] = threshold
        seen["filter"] = content_filter
        return list(results), []

    agent = _Agent()
    agent._content_filter_threshold = 0.25
    _run(agent, filter_tool_results=spy)
    assert seen["threshold"] == 0.25
    assert seen["filter"] is agent.content_filter


# ── The instrumentation ──────────────────────────────────────────────────────


def test_the_latency_events_are_emitted_with_the_batch_span() -> None:
    events: list = []
    _run(
        _Agent(results=["r"]),
        tool_uses=[{"name": "read_file"}],
        trace_latency=lambda name, **fields: events.append((name, fields)),
        interaction_log=lambda name, **fields: events.append((name, fields)),
    )
    names = [name for name, _fields in events]
    assert names == ["tool_uses_finished", "tool_batch_finished"]


def test_the_latency_event_splits_spawn_calls_from_regular_ones() -> None:
    """Which tool was orchestration is what the metric is for."""
    seen: dict = {}
    _run(
        _Agent(results=["r", "r"]),
        tool_uses=[{"name": "spawn_agent"}, {"name": "read_file"}],
        trace_latency=lambda name, **fields: seen.update(fields),
    )
    assert seen["spawn_calls"] == 1
    assert seen["regular_calls"] == 1


def test_instrumentation_is_optional() -> None:
    """A caller with no trace sink still runs the batch."""
    outcome, _ = _run(_Agent(), trace_latency=None, interaction_log=None)
    assert outcome.results


def test_the_orchestration_decision_reaches_the_runner() -> None:
    seen: dict = {}

    class _Recorder(_Agent):
        async def _run_tool_uses(self, tool_uses, orchestration_decision=None):
            seen["decision"] = orchestration_decision
            return list(self.results)

    _run(_Recorder(), orchestration_decision="the-decision")
    assert seen["decision"] == "the-decision"


def test_an_outcome_is_frozen() -> None:
    with pytest.raises(Exception):
        ToolBatchOutcome().restart_message = "other"  # type: ignore[misc]

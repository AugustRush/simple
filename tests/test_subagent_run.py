"""Driving a sub-agent: the retry policy and its wall-clock budget.

This was the hardest part of ``_execute_agent`` to test, because reaching it
meant running a real sub-agent through a real model.  The loop now takes the
function that performs one attempt, so the policy is exercised with a scripted
callable -- and the policy is where the surprising rules live:

- ``sub_agent_timeout_seconds`` bounds the **whole subtask**, not each attempt,
  so a retry cannot multiply the wall clock it is allowed to consume;
- a timeout ends the loop rather than retrying, because it has already spent the
  budget it would retry with;
- only retry-safe profiles retry at all.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agent.core.subagent_run import (
    AttemptOutcome,
    retries_for_profile,
    run_with_retries,
)


class _Sent:
    """A stand-in result object, shaped like ``AgentResult``."""

    def __init__(self, content: str = "ok") -> None:
        self.content = content
        self.error = None
        self.tool_calls_made: list[str] = []


class _Context:
    def __init__(self, messages: list | None = None) -> None:
        self.messages = messages if messages is not None else []


def _run(
    *,
    sends,
    profile: str = "read_only",
    retries: int = 2,
    timeout: float = 30.0,
    contexts=None,
):
    """Drive the loop with a scripted ``sends`` list.

    Each entry is either an exception to raise or a value to return.
    """
    calls: list[int] = []
    made: list[_Context] = []

    async def send():
        index = len(calls)
        calls.append(index)
        outcome = sends[index] if index < len(sends) else sends[-1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def fresh() -> _Context:
        ctx = _Context()
        if contexts is not None:
            contexts.append(ctx)
        made.append(ctx)
        return ctx

    current = {"ctx": None}

    def fresh_and_track() -> _Context:
        ctx = fresh()
        current["ctx"] = ctx
        return ctx

    # The first context exists before the loop, mirroring the real caller.
    current["ctx"] = fresh_and_track()

    outcome = asyncio.run(
        run_with_retries(
            role="researcher",
            shaped_task="task",
            capability_profile=profile,
            write_scope_count=0,
            configured_retries=retries,
            timeout_seconds=timeout,
            send=send,
            fresh_context=fresh_and_track,
            current_context=lambda: current["ctx"],
            emit=lambda **fields: None,
            format_error=lambda exc: f"failed: {exc}",
        )
    )
    return outcome, calls, made


# ── Retry safety ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("profile", ["read_only", "research"])
def test_retry_safe_profiles_use_the_configured_count(profile: str) -> None:
    assert retries_for_profile(profile, 3) == 3


@pytest.mark.parametrize("profile", ["implementation", "full", "anything-else"])
def test_other_profiles_never_retry(profile: str) -> None:
    """An implementation worker may have written files a retry cannot roll back."""
    assert retries_for_profile(profile, 3) == 0


def test_a_negative_configured_count_is_clamped() -> None:
    assert retries_for_profile("read_only", -5) == 0


# ── Success and failure ──────────────────────────────────────────────────────


def test_a_first_attempt_success_needs_one_call() -> None:
    outcome, calls, _ = _run(sends=[_Sent("done")])
    assert len(calls) == 1
    assert outcome.result.content == "done"
    assert outcome.payload is None


def test_transient_failures_are_retried_up_to_the_limit() -> None:
    outcome, calls, _ = _run(
        sends=[RuntimeError("503"), RuntimeError("503"), _Sent("finally")],
        retries=2,
    )
    assert len(calls) == 3
    assert outcome.result.content == "finally"


def test_a_success_after_failures_clears_the_failure_payload() -> None:
    """Otherwise the caller reports the *earlier* attempt's error.

    The caller reads ``payload`` whenever it is set, so a stale one turns a
    successful subtask into a reported failure.
    """
    outcome, _calls, _ = _run(
        sends=[RuntimeError("503"), _Sent("ok")], retries=2
    )
    assert outcome.payload is None
    assert outcome.result.content == "ok"


def test_exhausting_the_retries_reports_the_last_failure() -> None:
    outcome, calls, _ = _run(
        sends=[RuntimeError("first"), RuntimeError("second")], retries=1
    )
    assert len(calls) == 2
    assert outcome.result is None
    assert outcome.payload is not None
    assert "second" in outcome.payload["error"]


def test_a_non_retry_safe_profile_fails_without_a_second_call() -> None:
    outcome, calls, _ = _run(
        sends=[RuntimeError("boom")], profile="implementation", retries=3
    )
    assert len(calls) == 1
    assert outcome.payload is not None


# ── The budget ───────────────────────────────────────────────────────────────


def test_a_timeout_ends_the_loop_instead_of_retrying() -> None:
    """It already spent the budget it would retry with."""

    async def slow_send_never_used():
        raise AssertionError

    # A send that hangs lets wait_for time out.
    calls: list[int] = []

    async def hanging_send():
        calls.append(1)
        await asyncio.sleep(5)
        return _Sent()

    current = {"ctx": _Context()}
    started = time.monotonic()
    outcome = asyncio.run(
        run_with_retries(
            role="r",
            shaped_task="t",
            capability_profile="read_only",
            write_scope_count=0,
            configured_retries=3,
            timeout_seconds=0.05,
            send=hanging_send,
            fresh_context=lambda: current["ctx"],
            current_context=lambda: current["ctx"],
            emit=lambda **fields: None,
            format_error=str,
        )
    )
    elapsed = time.monotonic() - started

    assert len(calls) == 1, "a timeout must not be retried"
    assert outcome.payload is not None
    assert outcome.payload["timed_out"] is True
    assert elapsed < 1.0, "the budget must bound the whole subtask"


def test_the_timeout_message_names_the_whole_budget() -> None:
    """Not a per-attempt figure, which would understate the deadline."""

    async def hanging_send():
        await asyncio.sleep(5)
        return _Sent()

    current = {"ctx": _Context()}
    outcome = asyncio.run(
        run_with_retries(
            role="r",
            shaped_task="t",
            capability_profile="read_only",
            write_scope_count=0,
            configured_retries=3,
            timeout_seconds=0.05,
            send=hanging_send,
            fresh_context=lambda: current["ctx"],
            current_context=lambda: current["ctx"],
            emit=lambda **fields: None,
            format_error=str,
        )
    )
    assert "0.05s budget" in outcome.payload["error"]


def test_partial_text_from_the_timed_out_attempt_is_salvaged() -> None:
    """A sub-agent that said something before being cut off is more useful."""

    async def hanging_send():
        await asyncio.sleep(5)
        return _Sent()

    context = _Context(
        [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "I found three things"},
        ]
    )
    outcome = asyncio.run(
        run_with_retries(
            role="r",
            shaped_task="t",
            capability_profile="read_only",
            write_scope_count=0,
            configured_retries=0,
            timeout_seconds=0.05,
            send=hanging_send,
            fresh_context=lambda: context,
            current_context=lambda: context,
            emit=lambda **fields: None,
            format_error=str,
        )
    )
    assert outcome.payload["partial_content"] == "I found three things"


def test_no_partial_text_is_omitted_rather_than_empty() -> None:
    async def hanging_send():
        await asyncio.sleep(5)
        return _Sent()

    context = _Context([{"role": "user", "content": "go"}])
    outcome = asyncio.run(
        run_with_retries(
            role="r",
            shaped_task="t",
            capability_profile="read_only",
            write_scope_count=0,
            configured_retries=0,
            timeout_seconds=0.05,
            send=hanging_send,
            fresh_context=lambda: context,
            current_context=lambda: context,
            emit=lambda **fields: None,
            format_error=str,
        )
    )
    assert "partial_content" not in outcome.payload


# ── Retry context isolation ──────────────────────────────────────────────────


def test_each_retry_gets_a_fresh_context() -> None:
    """A retry must not inherit a failed run's messages.

    ``fresh_context`` is called once per **retry**, not once per attempt: the
    caller builds the first attempt's context before entering the loop (it needs
    one to report partial text from), and the loop only replaces it when an
    attempt has to be redone.  Asserted as the exact count because the
    alternative reading -- "called for every attempt" -- would double-build the
    first context, which is the sort of difference no other test would notice.
    """
    contexts: list[_Context] = []
    outcome, calls, made = _run(
        sends=[RuntimeError("503"), RuntimeError("503"), _Sent("ok")],
        retries=2,
        contexts=contexts,
    )
    assert len(calls) == 3, "three attempts"
    assert len(made) == 3, "one before the loop, one per retry -- two retries"
    assert len({id(ctx) for ctx in made}) == 3, "no context is reused"


def test_the_loop_reports_how_many_attempts_it_made() -> None:
    outcome, _calls, _ = _run(sends=[_Sent("ok")])
    assert outcome.attempts == 1


def test_an_attempt_outcome_is_frozen() -> None:
    with pytest.raises(Exception):
        AttemptOutcome().attempts = 5  # type: ignore[misc]


# ── The agent still delegates ────────────────────────────────────────────────


def test_the_agent_still_runs_sub_agents_through_the_loop() -> None:
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    parent = agent_module.BaseAgent(
        object(), registry, model="m", api_format="openai"
    )
    parent.sub_agent_timeout_seconds = 10
    parent.sub_agent_retries = 1
    parent._base_system_prompt = "sys"
    calls: list[int] = []

    class _Flaky:
        async def send_message(self, ctx, task):
            calls.append(1)
            if len(calls) < 2:
                raise RuntimeError("transient")
            return agent_module.AgentResult(
                agent_id="sub", content="recovered", tool_calls_made=[], error=None
            )

    parent._create_sub_agent = lambda sub_registry: _Flaky()
    payload = asyncio.run(
        parent._execute_agent(
            role="researcher", task="read docs", capability_profile="read_only"
        )
    )
    assert len(calls) == 2
    assert payload["ok"] is True
    assert payload["content"] == "recovered"

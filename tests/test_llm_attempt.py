"""Getting one model response for a turn.

Three ways, and only one of them calls a model.  The interesting rules are all in
the third: a streaming retry must not replay text the sink cannot take back, and
a *user* cancellation must not be swallowed by the retry machinery that exists to
swallow transient failures.

The extraction's own hazard is pinned here too: the block's early return had to
become a field on a result object, because a ``return`` inside an extracted
function would leave the caller's outer ``finally`` unrun -- silently skipping
three contextvar resets.
"""

from __future__ import annotations

import asyncio

import pytest

from agent.core.llm_attempt import LLMAttempt, obtain_response
from agent.core.stream_tracking import StreamEmissionTracker


class _Ctx:
    agent_id = "a1"
    metadata: dict = {}
    # The model-call leg builds a request out of these, so a context stand-in has to
    # carry them: the stub used to intercept before the request was assembled, and
    # that is exactly what it no longer does.
    system_prompt = "sys"
    messages: list = []


def _stub_model_call(agent):
    """The model-call surface this stub stands in for.

    ``obtain_response`` reaches the model through a :class:`ModelCall` now, so a
    stub that only overrode ``_create`` no longer intercepts anything.  Declaring
    the surface here rather than injecting a bare callable keeps the stub honest:
    it says what it is a stand-in *for*.
    """
    from agent.core.model_call import ModelCall

    class _Transport:
        async def create(self, **kwargs):
            agent.calls.append("create")
            if agent.raises:
                raise agent.raises
            return agent.result

    return ModelCall(
        transport=_Transport(),
        effective_model=lambda ctx=None: "m",
        configured_output_cap=lambda value=None: 16,
        prepare_provider_context=lambda ctx, tools, **kwargs: None,
        observe_provider_usage=lambda ctx, response: None,
        event_bus=None,
    )


def _stub_retry_policy():
    """The stub is standing in for an agent, so it owes the surface
    ``obtain_response`` reads.  The *behaviour* under test is injected separately
    (``retry=``), because these tests are about cancellation rather than backoff."""
    from agent.core.retry import RetryPolicy

    return RetryPolicy(max_retries=0, base_delay=0.0, event_bus=None)


class _Agent:
    """A collaborator that records how it was asked."""

    def __init__(self, result=None, raises=None) -> None:
        self.result = result if result is not None else "RESPONSE"
        self.raises = raises
        self.calls: list[str] = []
        self._retry_policy = _stub_retry_policy
        self._model_call = lambda: _stub_model_call(self)

    async def _create(self, ctx, tools):
        self.calls.append("create")
        if self.raises:
            raise self.raises
        return self.result

    async def _stream_response(self, ctx, tools, callback, reasoning, **kw):
        self.calls.append("stream")
        if self.raises:
            raise self.raises
        if callback is not None:
            callback("chunk")
        return (self.result, "streamed-text")



async def _passthrough(policy, fn, *args, **kwargs):
    """The retry leg without retrying: these tests are about cancellation, not backoff."""
    return await fn(*args, **kwargs)


def _run(agent, **overrides):
    kwargs = dict(
        agent=agent,
        retry=_passthrough,
        ctx=_Ctx(),
        tools=[],
        user_message="hello",
        recovered=None,
        pending_tool_response=None,
        stream_callback=None,
        trace_status="ok",
        trace_error=None,
        result_text="",
        tool_calls_made=[],
        active_sink=None,
        cancel_token=None,
    )
    kwargs.update(overrides)
    return asyncio.run(obtain_response(**kwargs))


# ── The three paths ──────────────────────────────────────────────────────────


def test_a_recovered_response_makes_no_call() -> None:
    """It is a replay of text the provider already produced."""
    agent = _Agent()
    outcome = _run(agent, recovered="RECOVERED")
    assert outcome.response == "RECOVERED"
    assert outcome.streamed_text == ""
    assert agent.calls == [], "a replay must not call the model"


def test_a_pending_tool_response_makes_no_call() -> None:
    agent = _Agent()
    outcome = _run(agent, pending_tool_response="PENDING")
    assert outcome.response == "PENDING"
    assert agent.calls == []


def test_a_pending_response_is_consumed() -> None:
    """The caller must see it cleared or it is submitted twice."""
    outcome = _run(_Agent(), pending_tool_response="PENDING")
    assert outcome.pending_tool_response is None


def test_a_pending_response_is_left_alone_on_the_replay_path() -> None:
    """Only the pending branch consumes it."""
    outcome = _run(_Agent(), recovered="R", pending_tool_response="P")
    assert outcome.pending_tool_response == "P"


def test_a_fresh_call_happens_when_there_is_nothing_to_replay() -> None:
    agent = _Agent()
    outcome = _run(agent)
    assert outcome.response == "RESPONSE"
    assert agent.calls == ["create"]


def test_a_streaming_call_goes_through_stream_response() -> None:
    agent = _Agent()
    outcome = _run(agent, stream_callback=lambda chunk: None)
    assert agent.calls == ["stream"]
    assert outcome.response == "RESPONSE"
    assert outcome.streamed_text == "streamed-text"


def test_a_plain_call_reports_no_streamed_text() -> None:
    outcome = _run(_Agent())
    assert outcome.streamed_text == ""


# ── Cancellation ─────────────────────────────────────────────────────────────


class _Token:
    def __init__(self, cancelled: bool) -> None:
        self.is_cancelled = cancelled
        self.cleanups: list[str] = []

    def register_cleanup(self, name, fn):
        self.cleanups.append(name)
        return lambda: None


def test_a_user_cancellation_becomes_an_early_result() -> None:
    """Not an exception: the caller must return it, and its ``finally`` must run.

    This is the reason the block returns a result object.  A ``return`` inside an
    extracted function would exit the helper, leaving ``send_message``'s outer
    ``finally`` -- which resets three contextvars -- unrun.
    """
    token = _Token(cancelled=True)

    async def cancelling(*args, **kwargs):
        raise asyncio.CancelledError

    agent = _Agent()
    outcome = _run(agent, cancel_token=token, retry=cancelling)

    assert outcome.early_result is not None
    assert outcome.trace_status == "cancelled"
    assert outcome.trace_error == "Turn cancelled by user"
    assert outcome.early_result.error == "Turn cancelled by user"
    assert outcome.early_result.tool_calls_made == []


def test_a_cancellation_nobody_asked_for_is_reraised() -> None:
    """The retry machinery must not swallow a cancellation from elsewhere."""
    token = _Token(cancelled=False)

    async def cancelling(*args, **kwargs):
        raise asyncio.CancelledError

    agent = _Agent()
    with pytest.raises(asyncio.CancelledError):
        _run(agent, cancel_token=token, retry=cancelling)


def test_the_llm_cleanup_is_registered_and_released() -> None:
    token = _Token(cancelled=False)
    _run(_Agent(), cancel_token=token)
    assert token.cleanups == ["llm_call"], "registered so /now can abort it"


def test_no_cancel_token_skips_registration() -> None:
    """A turn without a token has nothing to register against, and still works."""
    assert _run(_Agent()).response == "RESPONSE"


# ── The trace fields are threaded ────────────────────────────────────────────


def test_the_incoming_trace_state_is_preserved() -> None:
    """They are loop state this step may change, not locals it may reset."""
    outcome = _run(_Agent(), trace_status="ok", trace_error=None)
    assert outcome.trace_status == "ok"
    assert outcome.trace_error is None


def test_an_earlier_trace_error_survives_a_successful_step() -> None:
    """A step that succeeds must not clear a failure recorded before it."""
    outcome = _run(_Agent(), trace_status="error", trace_error="earlier")
    assert outcome.trace_status == "error"
    assert outcome.trace_error == "earlier"


def test_the_cancellation_text_falls_back_to_a_shared_message() -> None:
    token = _Token(cancelled=True)

    async def cancelling(*args, **kwargs):
        raise asyncio.CancelledError

    agent = _Agent()
    outcome = _run(agent, cancel_token=token, result_text="", retry=cancelling)
    assert outcome.early_result.content, "an empty reply would be useless"


def test_partial_text_is_kept_in_the_cancellation_result() -> None:
    token = _Token(cancelled=True)

    async def cancelling(*args, **kwargs):
        raise asyncio.CancelledError

    agent = _Agent()
    outcome = _run(agent, cancel_token=token, result_text="half an answer", retry=cancelling)
    assert outcome.early_result.content == "half an answer"


# ── The tracker still guards streaming retries ───────────────────────────────


def test_the_tracker_reports_nothing_emitted_before_a_chunk() -> None:
    tracker = StreamEmissionTracker(lambda chunk: None)
    assert tracker.nothing_emitted() is True
    tracker("chunk")
    assert tracker.nothing_emitted() is False


def test_an_empty_chunk_does_not_count_as_emitted() -> None:
    """An empty string reaches the sink and shows nothing."""
    tracker = StreamEmissionTracker(lambda chunk: None)
    tracker("")
    assert tracker.nothing_emitted() is True


def test_the_reasoning_channel_counts_against_the_same_flag() -> None:
    """Thinking is exactly what a thinking-heavy turn emitted when it died."""
    tracker = StreamEmissionTracker(lambda chunk: None)
    reasoning = tracker.wrap_reasoning(lambda chunk: None)
    reasoning("thinking...")
    assert tracker.nothing_emitted() is False


def test_the_tracker_forwards_what_it_wraps() -> None:
    seen: list[str] = []
    tracker = StreamEmissionTracker(seen.append)
    tracker("a")
    reasoning = tracker.wrap_reasoning(seen.append)
    reasoning("b")
    assert seen == ["a", "b"]


def test_an_attempt_is_frozen() -> None:
    with pytest.raises(Exception):
        LLMAttempt().response = "other"  # type: ignore[misc]

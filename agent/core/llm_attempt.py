"""Getting one model response for a turn.

Why this is its own module
--------------------------
This was the 78-line head of ``send_message``'s per-step ``try``: three ways to
obtain a response, of which only the third calls a model.

1. **A recovered response.**  The previous turn was cut off by the provider's
   content filter and the recovered text is being replayed, so no call is made.
2. **A pending tool response.**  A tool result was already produced and is being
   submitted on this step instead.
3. **A fresh call.**  The model is asked, streamed if the caller wants streaming,
   retried under the LLM retry policy, and registered with the cancel token so
   ``/now`` can abort the HTTP request in flight.

Only the third is interesting, and it is the most important path in the agent --
which is precisely why it was worth naming rather than leaving as the opening
paragraph of a 572-line method.

Why it returns a result object instead of returning the result
--------------------------------------------------------------
The block contains an early ``return AgentResult`` for a user cancellation, and
that return must still leave ``send_message``'s outer ``finally`` to run (three
contextvars are reset there).  Extracting a function and calling ``return`` inside
it would instead return from the *helper*, silently skipping that cleanup.  So the
helper reports the outcome and the caller decides: :class:`LLMAttempt` carries
either a response to use or an ``AgentResult`` to return as-is.

The trace fields are threaded rather than recomputed
----------------------------------------------------
``trace_status`` and ``trace_error`` are seeded before the loop and read after it,
so a cancellation recorded here has to reach the caller's ``AgentResult``.  They
are passed in and returned back for that reason -- they are loop state that this
step is allowed to change, not locals.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Optional

from agent.core.stream_tracking import StreamEmissionTracker as _StreamEmissionTracker

__all__ = ["LLMAttempt", "obtain_response"]

#: Returned by ``llm_abort`` and the retry helper when there is nothing to undo.
_NOOP: Callable[[], None] = lambda: None  # noqa: E731


@dataclass(frozen=True)
class LLMAttempt:
    """What one step's response acquisition produced.

    ``early_result`` is set only for a user cancellation, in which case the other
    fields are not meaningful and the caller must return it directly.  ``response``
    and ``streamed_text`` are the normal outcome; ``pending_tool_response`` comes
    back because the second branch consumes it and the caller must see it cleared.
    """

    response: Any = None
    streamed_text: str = ""
    pending_tool_response: Any = None
    trace_status: str = "ok"
    trace_error: Optional[str] = None
    early_result: Any = None


async def obtain_response(
    *,
    agent: Any,
    ctx: Any,
    tools: list,
    user_message: str,
    recovered: Any = None,
    pending_tool_response: Any = None,
    stream_callback: Any = None,
    trace_status: str = "ok",
    trace_error: Optional[str] = None,
    result_text: str = "",
    tool_calls_made: Optional[list] = None,
    active_sink: Any = None,
    cancel_token: Any = None,
) -> LLMAttempt:
    """Obtain this step's response, by replay, by resubmission, or by calling.

    ``agent`` is the collaborator providing ``_create``, ``_stream_response`` and
    ``_with_llm_retry``; this module deliberately does not import
    ``agent.core.agent``, which is where it is used from.
    """
    # 1. A response recovered from a content-filtered turn: no call to make.
    if recovered is not None:
        return LLMAttempt(
            response=recovered,
            streamed_text="",
            pending_tool_response=pending_tool_response,
            trace_status=trace_status,
            trace_error=trace_error,
        )

    # 2. A tool result already produced and being submitted on this step.
    if pending_tool_response is not None:
        return LLMAttempt(
            response=pending_tool_response,
            streamed_text="",
            pending_tool_response=None,
            trace_status=trace_status,
            trace_error=trace_error,
        )

    # 3. Call the model.  Wrapped as a task with a cleanup registered so a
    #    force-cancel aborts the HTTP request mid-flight; a graceful cancel lets
    #    the call finish on its own.
    if stream_callback:
        tracked_callback = _StreamEmissionTracker(stream_callback)
        # The reasoning channel is resolved here rather than inside
        # ``_stream_response`` so the tracker can wrap it: reasoning arrives
        # ahead of the first answer token, so it is exactly the emission that has
        # to veto a replay.
        raw_reasoning = (
            getattr(active_sink, "sync_reasoning_cb", None)
            if active_sink is not None
            else None
        )
        reasoning_callback = (
            tracked_callback.wrap_reasoning(raw_reasoning)
            if raw_reasoning is not None
            else None
        )
        llm_task = asyncio.create_task(
            agent._with_llm_retry(
                agent._stream_response,
                ctx,
                tools,
                tracked_callback,
                reasoning_callback,
                can_retry=tracked_callback.nothing_emitted,
            )
        )
    else:
        tracked_callback = None
        llm_task = asyncio.create_task(
            agent._with_llm_retry(agent._create, ctx, tools)
        )

    def _abort_llm(level: str) -> None:
        if not llm_task.done():
            llm_task.cancel()

    _llm_deregister = (
        cancel_token.register_cleanup("llm_call", _abort_llm)
        if cancel_token is not None
        else _NOOP
    )
    try:
        try:
            llm_result = await llm_task
        except asyncio.CancelledError:
            # A canceller that did not ask for this is not ours to swallow.
            if cancel_token is None or not cancel_token.is_cancelled:
                raise
            from agent.core.agent import AgentResult

            return LLMAttempt(
                pending_tool_response=pending_tool_response,
                trace_status="cancelled",
                trace_error="Turn cancelled by user",
                early_result=AgentResult(
                    agent_id=ctx.agent_id,
                    content=(
                        result_text
                        or _cancelled_text(user_message)
                    ),
                    tool_calls_made=tool_calls_made or [],
                    error="Turn cancelled by user",
                ),
            )
    finally:
        _llm_deregister()

    if stream_callback:
        response, streamed_text = llm_result
    else:
        response, streamed_text = llm_result, ""

    return LLMAttempt(
        response=response,
        streamed_text=streamed_text,
        pending_tool_response=pending_tool_response,
        trace_status=trace_status,
        trace_error=trace_error,
    )


def _cancelled_text(user_message: str) -> str:
    from agent import shared

    return shared._cancelled_by_user_text(user_message)

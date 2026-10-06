"""Running one batch of tool calls and deciding what to do with the results.

Why this is its own module
--------------------------
This was 68 lines in the middle of ``send_message``'s tool-use branch, and it is
one operation with one decision at the end: run the batch, screen the output
against the provider's content filter, and decide whether the conversation can
continue as-is or has to be restarted with the offending results summarized.

That decision is the interesting part.  A provider can reject a request because of
what a *tool result* contained, and the only way back is to drop the whole
protocol unit and resend it with the flagged output replaced.  So this returns a
``restart_message`` rather than performing the restart: the caller owns
``ctx.messages`` and the loop counter, and a helper that mutated them would be
performing the caller's control flow.

Why the assistant-text token is set and reset here
--------------------------------------------------
The token makes the turn's assistant text visible to tools that ask for it, and
it is reset in a ``finally`` so a failed batch does not leave a stale value behind
for the next step.  Both halves belong with the execution -- splitting them would
mean the reset could be skipped by a caller that forgot.

The instrumentation travels with the operation
----------------------------------------------
The two latency events and the interaction log are here because they measure
exactly this span.  Leaving them at the call site would mean timing a call whose
start had to be passed in, which is how durations drift from what they name.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = ["ToolBatchOutcome", "run_tool_batch"]


@dataclass(frozen=True)
class ToolBatchOutcome:
    """What a tool batch produced.

    ``results`` is the raw tool output.  It comes back because the caller needs it
    after this step -- to record what was submitted for rollback, to feed loop
    detection, and to report remaining work -- and re-deriving it is impossible.

    ``filtered_results`` is what may be submitted to the provider;
    ``risky_indices`` says which entries were replaced, for the caller's logging.

    ``restart_message`` is set when the batch cannot be submitted as-is.  When it
    is not ``None`` the caller must replace the conversation, forget the recorded
    submission, advance its step counter, and continue -- those are the caller's
    state, and this reports the decision rather than taking it.
    """

    results: list = field(default_factory=list)
    filtered_results: list = field(default_factory=list)
    risky_indices: list = field(default_factory=list)
    restart_message: Optional[str] = None


async def run_tool_batch(
    *,
    agent: Any,
    ctx: Any,
    tool_uses: list,
    orchestration_decision: Any = None,
    intent_text: str = "",
    tool_result_history: Optional[list] = None,
    step_index: int = 0,
    watchdog: Any = None,
    interaction_log: Any = None,
    trace_latency: Any = None,
    logger: Any = None,
    filter_tool_results: Any = None,
    active_assistant_text: Any = None,
) -> ToolBatchOutcome:
    """Run *tool_uses*, screen the results, and report the outcome.

    ``agent`` is the collaborator providing ``_run_tool_uses``,
    ``_clear_context_restart_message``, ``content_filter``,
    ``_content_filter_threshold`` and ``registry``; this module does not import
    ``agent.core.agent``.

    ``intent_text`` is what the tools see when they ask for the turn's assistant
    text -- the parsed text if there is any, else whatever was streamed.  It is
    passed in because the caller is the one that knows the difference between
    "the model produced no text" and "the text was streamed but not parsed".

    ``tool_calls_made`` is *not* taken here: the caller appends the batch's tool
    names to it, because that list outlives this step and is reported in the
    final result.

    ``tool_result_history`` is appended to when given, because the summary pairs
    belong to this batch and the caller only needs the accumulated history at the
    end of the turn.  It is passed in rather than returned because it is a running
    log across every step, not this step's output.
    """
    started_at = time.perf_counter()
    tool_names = ", ".join(
        tu["name"] for tu in tool_uses[:3]
    ) + (f" +{len(tool_uses) - 3}" if len(tool_uses) > 3 else "")

    # Make the turn's assistant text visible to tools that ask for it, and put it
    # back whatever happens: a stale value would leak into the next step.
    # A separate flag, not a truthiness test on the token: ``set`` returns the
    # *previous* value, which is legitimately ``None`` when nothing was set
    # before -- so guarding on the token's value skips the reset exactly when the
    # channel was clean, which is the common case.
    published = active_assistant_text is not None
    intent_token = active_assistant_text.set(intent_text) if published else None
    try:
        results = await agent._run_tool_uses(
            tool_uses,
            orchestration_decision=orchestration_decision,
        )
    finally:
        if published:
            active_assistant_text.reset(intent_token)

    _emit_tool_metrics(
        agent=agent,
        ctx=ctx,
        tool_uses=tool_uses,
        step_index=step_index,
        started_at=started_at,
        trace_latency=trace_latency,
        interaction_log=interaction_log,
    )

    if tool_result_history is not None:
        tool_result_history.extend(
            (tu["name"], agent._extract_summary_text(res))
            for tu, res in zip(tool_uses, results)
        )

    # Screen results before API submission: a flagged result is summarized so it
    # does not trip the provider's content policy on the next call.
    filtered_results, risky_indices = filter_tool_results(
        agent.content_filter,
        tool_uses,
        results,
        threshold=agent._content_filter_threshold,
    )
    if risky_indices:
        await agent.content_filter.learn_and_persist(
            [results[i] for i in risky_indices]
        )
        if logger is not None:
            logger.info(
                "Content filter flagged %d/%d tool results as risky: %s",
                len(risky_indices),
                len(results),
                [tool_uses[i]["name"] for i in risky_indices],
            )

    restart_message = agent._clear_context_restart_message(tool_uses, results)
    return ToolBatchOutcome(
        results=results,
        filtered_results=filtered_results,
        risky_indices=list(risky_indices),
        restart_message=restart_message,
    )


def _emit_tool_metrics(
    *,
    agent: Any,
    ctx: Any,
    tool_uses: list,
    step_index: int,
    started_at: float,
    trace_latency: Any,
    interaction_log: Any,
) -> None:
    """The two latency events and the batch log for one tool batch."""
    duration_ms = f"{(time.perf_counter() - started_at) * 1000:.1f}"
    spawn_calls = sum(
        1
        for tool_use in tool_uses
        if "orchestration" in agent.registry.tool_capabilities(tool_use["name"])
    )
    regular_calls = sum(
        1
        for tool_use in tool_uses
        if "orchestration" not in agent.registry.tool_capabilities(tool_use["name"])
    )
    if trace_latency is not None:
        trace_latency(
            "tool_uses_finished",
            agent_id=ctx.agent_id,
            step=step_index + 1,
            total_tool_uses=len(tool_uses),
            spawn_calls=spawn_calls,
            regular_calls=regular_calls,
            duration_ms=duration_ms,
        )
    if interaction_log is not None:
        interaction_log(
            "tool_batch_finished",
            agent_id=ctx.agent_id,
            step=step_index + 1,
            tool_uses=len(tool_uses),
            duration_ms=duration_ms,
        )

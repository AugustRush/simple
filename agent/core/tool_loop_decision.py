"""The tool-loop watchdog: has this turn stopped making progress?

Why this is its own module
--------------------------
``_check_tool_loop_stuck`` already knew *whether* a turn was stuck; what lived in
``send_message`` was what to do about it -- build the response the user sees,
append it to the conversation, record the trace status, log the watchdog's
verdict, and end the turn.  That is five effects for one decision, and it ended
the turn from inside a ``try`` whose ``finally`` resets three contextvars.

The early return is why this returns a decision instead
-------------------------------------------------------
A ``return`` inside an extracted function exits the *helper*.  The original
returned an ``AgentResult`` from ``send_message``, and that path still has to run
the outer ``finally``.  So :class:`StuckDecision` carries ``final_result`` and the
caller returns it -- the same pattern the response-acquisition path uses, for the
same reason.

The watchdog's own state is read, not owned
-------------------------------------------
``watchdog`` is a dict threaded through the turn and updated by
``_check_tool_loop_stuck``.  It is passed in so the verdict and the outcome it
records come from the same inspection; reading it here and checking it elsewhere
would be two views of one fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

__all__ = ["StuckDecision", "handle_stuck_loop"]


@dataclass(frozen=True)
class StuckDecision:
    """What to do about a tool loop that stopped making progress.

    ``final_result`` is set only when the turn must end, in which case the caller
    returns it.  ``text`` is the response that was also appended to the
    conversation, so the caller can use it without re-deriving it.
    """

    stuck: bool = False
    reason: str = ""
    text: str = ""
    final_result: Any = None


def handle_stuck_loop(
    *,
    agent: Any,
    ctx: Any,
    watchdog: Any,
    tool_uses: list,
    results: list,
    user_message: str,
    tool_calls_made: list,
    step_index: int = 0,
    trace_status: str = "ok",
    interaction_log: Any = None,
) -> StuckDecision:
    """Check the watchdog and, if the turn is stuck, end it.

    ``agent`` provides ``_check_tool_loop_stuck`` and
    ``_build_tool_loop_stuck_response``; this module does not import
    ``agent.core.agent``.

    ``trace_status`` comes back on the decision because a stuck turn sets it, and
    the caller reports it in the result it returns for the non-stuck paths too.

    Returns a decision with ``stuck=False`` for the ordinary case, which is the
    common path -- the check runs on every tool step and almost never fires.
    """
    reason = agent._check_tool_loop_stuck(watchdog, tool_uses, results)
    if not reason:
        return StuckDecision(stuck=False)

    from agent.core.agent import AgentResult

    text = agent._build_tool_loop_stuck_response(
        user_message=user_message,
        reason=reason,
        tool_uses=tool_uses,
        results=results,
    )
    # Appended before returning: the caller ends the turn here, and a response the
    # user saw but the conversation does not hold would be lost on replay.
    ctx.messages.append({"role": "assistant", "content": text})

    if interaction_log is not None:
        interaction_log(
            "tool_loop_watchdog_stopped",
            agent_id=ctx.agent_id,
            step=step_index + 1,
            reason=reason,
            tool_uses=len(tool_uses),
            tool_names=",".join(tu["name"] for tu in tool_uses[:5]),
            tool_outcome=watchdog.get("last_outcome", {}),
        )

    return StuckDecision(
        stuck=True,
        reason=reason,
        text=text,
        final_result=AgentResult(
            agent_id=ctx.agent_id,
            content=text,
            tool_calls_made=tool_calls_made,
        ),
    )

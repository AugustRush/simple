"""The turn loop's own helpers, as they move out of ``BaseAgent``.

Why this module exists
----------------------
The plan's third phase is "the agent loop is a plugin".  ``TurnRunner`` is the
plugin, but the loop *body* still lives in ``BaseAgent``; moving it wholesale is
what the extraction is for, and it is done leaf-first so that every step is small
enough to verify against the full suite.

What belongs here is decided by measurement, not by taste: a helper moves when the
turn loop is its **only** caller and the only things it needs from the agent are
services.  A helper with callers elsewhere stays on the agent, and the loop reaches
it through the agent it was given — which is how the six shared ones
(``_effective_model``, ``_context_manager_for``, ``_parse_response``,
``_format_agent_error``, ``_is_content_filter_block``, ``max_tool_call_iterations``)
are handled.

Why the transport is a parameter
--------------------------------
Both builders below are one call to the model adapter with a model name attached.
They read ``self._transport`` and ``self._effective_model(ctx)`` on the agent, which
made the dependency invisible: from the signature, a reader could not tell that
building a history entry involves the model adapter at all.  As parameters, the
adapter is a parameter like any other, and the caller chooses it — which is what
makes the adapter replaceable at the loop's edge rather than only at the agent's.

The model name is resolved by the caller rather than here, because
``_effective_model`` is shared with thirteen other callers and therefore still
belongs to the agent.  Passing the answer rather than the means to compute it keeps
this module free of a reference back to the agent.
"""

from __future__ import annotations

from typing import Any

__all__ = ["assistant_message", "tool_result_messages"]


def assistant_message(
    transport: Any, response: Any, text: str, model: str | None = None
) -> dict:
    """The assistant history entry after a ``tool_use`` stop.

    ``model`` is optional because the entry does not always need one: a caller with
    no context to resolve a per-turn override from passes nothing, and the adapter
    falls back to its own default.  That is the adapter's decision to make, not this
    function's.
    """
    return transport.build_assistant_message(response, text, model=model)


def tool_result_messages(
    transport: Any,
    tool_calls: list[dict],
    results: list[str],
    model: str | None = None,
) -> list[dict]:
    """Tool-result history entries, in the shape the adapter's format expects."""
    return transport.build_tool_result_messages(tool_calls, results, model=model)

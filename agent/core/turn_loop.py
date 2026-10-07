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

from dataclasses import dataclass
from typing import Any, Callable

__all__ = ["LoopDeps", "assistant_message", "tool_result_messages"]


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


# ── The loop's dependency surface ────────────────────────────────────────────
#
# Measured, not guessed: the set of members the turn loop and its helpers read from
# the agent, minus the ones only the loop uses (those move with it).  The
# measurement is repeated as a test, so a new ``self.X`` in the loop fails rather
# than silently widening the interface.
#
# Why a struct rather than twenty-odd parameters: passing them individually makes
# every loop function take a list that is mostly noise, and adding a dependency
# means editing each signature in turn.  Why a struct rather than the agent itself:
# a back-reference would let the loop reach *anything* on the agent, which is how an
# extraction quietly fails to decouple.  The point is that this list is closed.
#
# Values and callables are both here, and the difference matters when reading it:
# ``transport`` and ``registry`` are objects; ``effective_model`` and ``create_``
# are bound methods.  The struct holds bound methods, so it is valid only while the
# agent it came from is alive -- the same lifetime the loop body has today, and the
# reason ``of`` is called per turn rather than cached.


@dataclass(frozen=True)
class LoopDeps:
    """Everything the turn loop needs from the agent, and nothing else.

    Built with :meth:`of`.  Frozen so a loop iteration cannot reassign a dependency
    halfway through a turn, which would make the model or the retry budget depend on
    where in the loop it was read.
    """

    # The model adapter and the model name in effect.  ``transport`` is the ``llm``
    # service, already resolved by the agent that owns this struct.
    transport: Any
    effective_model: Callable[..., str]
    configured_output_cap: Callable[..., int]
    prepare_provider_context: Callable[..., Any]
    observe_provider_usage: Callable[..., Any]
    supports_vision: bool

    # The tool registry and the tool-loop ceiling.
    registry: Any
    max_tool_call_iterations: int

    # Context assembly and its budgets.  ``token_budget`` is a factory rather than a
    # value: the budget depends on the turn, so the loop asks for one when it needs
    # one instead of being handed a number that was right at some earlier point.
    context_assembler: Any
    context_manager_for: Callable[..., Any]
    bounded_text: Callable[..., Any]
    protected_turn_messages: Any
    input_token_budget: Callable[..., int]
    output_room: Callable[..., int]
    token_budget: Callable[..., Any]

    # Output limits and continuation.
    max_tokens: int
    max_truncation_continuations: int

    # Retry policy.
    llm_max_retries: int
    llm_retry_base_delay: float

    # Events: the two buses the loop publishes to.
    event_bus: Any
    llm_event_bus: Any

    # Content-filter recovery, and error formatting.
    content_filter: Any
    format_agent_error: Callable[..., Any]

    @classmethod
    def of(cls, agent: Any) -> "LoopDeps":
        """Read the surface off *agent*.

        Written out by hand rather than collected with ``getattr`` over a name list:
        a reflective version would accept a typo by failing later, and the one thing
        this struct is for is that the set is visible and closed.
        """
        return cls(
            transport=agent._transport,
            effective_model=agent._effective_model,
            configured_output_cap=agent._configured_output_cap,
            prepare_provider_context=agent._prepare_provider_context,
            observe_provider_usage=agent._observe_provider_usage,
            supports_vision=agent.supports_vision,
            registry=agent.registry,
            max_tool_call_iterations=agent.max_tool_call_iterations,
            context_assembler=agent.context_assembler,
            context_manager_for=agent._context_manager_for,
            bounded_text=agent._bounded_text,
            protected_turn_messages=agent._protected_turn_messages,
            input_token_budget=agent._input_token_budget,
            output_room=agent._output_room,
            token_budget=agent._token_budget,
            max_tokens=agent.max_tokens,
            max_truncation_continuations=agent.max_truncation_continuations,
            llm_max_retries=agent.llm_max_retries,
            llm_retry_base_delay=agent.llm_retry_base_delay,
            event_bus=agent._event_bus,
            llm_event_bus=agent._llm_event_bus,
            content_filter=agent.content_filter,
            format_agent_error=agent._format_agent_error,
        )

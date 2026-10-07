"""Making one model call, with the six things a call needs rather than an agent.

Same shape as :mod:`agent.core.retry`, and for the same reason: the first attempt at
moving this out handed it the whole loop surface (twenty-three members), which made
``agent/core/llm_attempt.py`` -- a module that documents it does not import
``agent.core.agent`` -- hold a complete agent.  A part of the loop declares *its*
dependencies and no more.

What a call needs, and why each is here
---------------------------------------
``transport``
    The adapter; the ``llm`` service, resolved by the agent that owns the struct.
``effective_model``
    The model name in effect *for this turn*, which is not the configured model: a
    per-call override in the turn's metadata wins, and only the agent knows how to
    read that.
``configured_output_cap``
    The ceiling when the request has not recorded a weighed one.
``prepare_provider_context``
    Which fills in the request and records the cap this call can actually afford.
``observe_provider_usage``
    Which folds the response's token usage back into the estimator.
``event_bus``
    The llm bus, for the request/response announcements.

The order of calls matters and is preserved: the context is prepared *before* the
model name is read, because preparation is what records the weighed cap that the
call then pops -- a call that read the name first would still be correct here but
would hide the dependency if preparation ever started affecting the name.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from agent.core.event_domains import LLMRequestStarted, LLMResponseReceived
from agent.core.turn_context import active_agent_id, emit_domain_event

__all__ = ["ModelCall", "create_turn_response"]


@dataclass(frozen=True)
class ModelCall:
    """The six things one model call needs from the agent.

    Frozen for the same reason as :class:`~agent.core.retry.RetryPolicy`: a call that
    could be re-pointed at another adapter mid-turn would make *which provider
    answered* depend on where in the loop it was read.
    """

    transport: Any
    effective_model: Callable[..., str]
    configured_output_cap: Callable[..., int]
    prepare_provider_context: Callable[..., Any]
    observe_provider_usage: Callable[..., Any]
    event_bus: Any


async def create_turn_response(
    call: ModelCall,
    ctx: Any,
    tools: list[dict],
    *,
    output_max_tokens: int | None = None,
) -> Any:
    """Non-streaming API call, returns a normalised response object."""
    # `output_max_tokens` is forwarded unresolved on purpose; see
    # `configured_output_cap`.
    call.prepare_provider_context(ctx, tools, output_max_tokens=output_max_tokens)
    model = call.effective_model(ctx)
    await emit_domain_event(
        call.event_bus,
        LLMRequestStarted,
        agent_id=active_agent_id(),
        model=model,
        tools_offered=len(tools or ()),
    )
    started_at = time.perf_counter()
    response = await call.transport.create(
        model=call.effective_model(ctx),
        # Not the configured cap: the cap this call can afford depends on what the
        # request actually weighs, which only `prepare_provider_context` knows.  The
        # fallback covers the case where it returned before recording one.
        max_tokens=int(
            ctx.metadata.pop(
                "_output_cap", call.configured_output_cap(output_max_tokens)
            )
        ),
        system=ctx.system_prompt,
        messages=ctx.messages,
        tools=tools,
    )
    call.observe_provider_usage(ctx, response)
    await emit_domain_event(
        call.event_bus,
        LLMResponseReceived,
        agent_id=active_agent_id(),
        duration_ms=(time.perf_counter() - started_at) * 1000,
    )
    return response

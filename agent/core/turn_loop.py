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

import base64
import html
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional

from agent.config import _now
from agent.core.attachments import MessageAttachment, format_attachment_context
from agent.core.turn_context import TURN_REQUEST_KEY
from agent.orchestration.planner import OrchestrationDecision, OrchestrationPlanner
from agent.skills.catalog import SkillCatalog

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agent.core.agent import AgentContext

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
def prepare_turn(
        deps: "LoopDeps",
        ctx: "AgentContext",
        user_message: str,
        attachments: tuple[MessageAttachment, ...],
    ) -> OrchestrationDecision:
        """Mutate ctx for one turn: inject LTM context, activate skills, plan
        orchestration, and append the user message.  Returns the orchestration
        decision so the loop can route spawn calls correctly.

        Everything this turn adds that varies per turn is collected into
        ``turn_blocks`` and carried by the turn's own message, never by
        ``ctx.system_prompt``.  The system prompt is the head of the request, so
        a volatile value there caps what the provider can serve from its prefix
        cache for the rest of the session; in the message it is appended once
        and then frozen.  ``ctx.system_prompt`` is left exactly as it was found.
        """
        # What this turn was asked to do, in the asker's own words, published
        # for the tools that have to prove they are answering it (see
        # ``RegularToolExecutor._check_request``).  Overwritten unconditionally:
        # a value left over from the previous turn would let this turn borrow
        # that turn's authority, and the one case that must never pass is a turn
        # where nobody asked for anything.
        deps.registry.set_context("turn_request", user_message)
        # The interjections this turn answers, emptied at the boundary for the
        # same reason as `turn_request` above: `ctx.metadata` is the session's,
        # so a ledger left over from the previous turn would be journaled again
        # by this one.  Reset here rather than read-and-cleared at turn end
        # because it has to outlive every drain inside the turn: the mailbox is
        # drained once per step, and every round of the continuation loop calls
        # this method and then `_post_turn_maintenance` on the same ctx
        # (`runtime/contracts.py`), so the ledger is read more than once per
        # user message.  An interjection carrying no message id has no key for
        # its write to be idempotent on, so a stale ledger is a duplicate row in
        # the transcript and the same words staged into memory again.
        ctx.metadata["_turn_interjections"] = []
        turn_blocks: list[str] = []
        handoff_block = str(ctx.metadata.get("_handoff_block") or "").strip()
        if handoff_block:
            turn_blocks.append(handoff_block)
        checkpoint_summary = str(
            ctx.metadata.get("_checkpoint_summary") or ""
        ).strip()
        if checkpoint_summary:
            turn_blocks.append(
                '<session_checkpoint trust="untrusted_evidence">\n'
                + html.escape(checkpoint_summary, quote=False)
                + "\n</session_checkpoint>"
            )
        # retrieve_implicit_context() covers both recent staging-buffer turns
        # (not yet consolidated) and historical LTM hits.  Skipping the
        # staging side would let recently-compacted turns drop from view.
        decision = plan_orchestration(deps, ctx, user_message)
        all_tools = deps.registry.to_anthropic_format() if ctx.tools_enabled else []
        # The schemas are the same for every call this agent instance makes --
        # see `ContextAssembler.select_tools` for why a per-turn set is the
        # expensive thing to do.  The one exception is the run's own report
        # channel, which refuses outside a run: shipping it in a conversation
        # would be shipping a tool whose only possible outcome is an error.
        # `scheduler_run_id` is fixed at context construction, so that gate is
        # a function of session-stable state too.
        ctx.metadata["_selected_tools"] = deps.context_assembler.select_tools(
            all_tools,
            scheduled_run=bool(ctx.metadata.get("scheduler_run_id")),
        )
        context_manager = deps.context_manager_for(ctx)
        # Retrieval's place in the block list is reserved now and filled in
        # below, because its *size* cannot be decided until every other block
        # that rides in this same message has been built: they share one window.
        retrieval_slot = len(turn_blocks)
        turn_blocks.append("")
        skill_catalog: Optional[SkillCatalog] = ctx.metadata.get("skill_catalog")
        required_skills: list[str] = list(ctx.metadata.get("required_skills", []))
        if skill_catalog and required_skills:
            active_blocks = []
            for skill_ref in required_skills:
                activation = skill_catalog.activation_text(skill_ref, explicit=True)
                if activation:
                    active_blocks.append(activation)
            if active_blocks:
                turn_blocks.append("## Active Skills\n" + "\n\n".join(active_blocks))
        if decision.mode == "explicit":
            policy = (
                "When using orchestration tools, encode ordering and coordination "
                "explicitly with depends_on and coordination_mode. Treat wording cues "
                "as advisory only; the explicit subtask graph is authoritative."
            )
            if decision.guidance:
                policy += " Advisory cues for this request: " + deps.bounded_text(
                    decision.guidance,
                    800,
                )
            turn_blocks.append("## Orchestration policy\n" + policy)
        # The time rides here rather than in the system prompt: it changes every
        # minute, and in the head that would cap the provider's cacheable prefix
        # at whatever precedes it for every request in the session.  Here it is
        # also more accurate -- the time of *this* turn, not of whichever turn
        # last re-rendered the prompt.
        turn_blocks.append(
            f"Current UTC time: {_now()}. "
            "Use the current_time tool when the user asks about local time or timezone conversions."
        )
        # Size retrieval against the message it will actually ride in.  Sizing it
        # from the raw user message charged the window for a message that is
        # never sent: what goes out is `turn_context + "\n\n" + user_message`, so
        # every token the blocks above added was invisible to the allocation and
        # retrieval was handed room that had already been spent -- which the
        # turn-boundary cut then had to take back out of older history.
        blocks_without_retrieval = "\n\n".join(
            block for block in turn_blocks if block
        )
        retrieval_budget = deps.token_budget.retrieval_token_budget(
            ctx,
            ctx.metadata.get("_selected_tools") or [],
            current_user_content=build_user_message_content(deps, 
                user_message, attachments, turn_context=blocks_without_retrieval
            ),
        )
        if context_manager and retrieval_budget > 0:
            has_active_state = True
            if ctx.metadata.get("_has_provider_checkpoint"):
                active_state = getattr(
                    context_manager, "has_active_working_state", None
                )
                if callable(active_state):
                    has_active_state = bool(active_state())
            retrieved = context_manager.retrieve_implicit_context(
                user_message,
                current_messages=ctx.messages,
                current_turn_id=str(ctx.metadata.get("turn_id") or ""),
                token_budget=retrieval_budget,
                include_recent_session=not bool(
                    ctx.metadata.get("_has_provider_checkpoint")
                ),
                include_working_state=has_active_state,
            )
            if retrieved:
                turn_blocks[retrieval_slot] = (
                    '<retrieved_context trust="untrusted_evidence">\n'
                    + html.escape(retrieved, quote=False)
                    + "\n</retrieved_context>"
                )
        request_message = {
            "role": "user",
            "content": build_user_message_content(deps, 
                user_message,
                attachments,
                turn_context="\n\n".join(block for block in turn_blocks if block),
            ),
        }
        ctx.messages.append(request_message)
        # Named so that a cut running later *inside this turn* can recognise it.
        # A message is identified by the object, not by how it reads, because an
        # interjection appended after it reads the same way.
        ctx.metadata[TURN_REQUEST_KEY] = request_message
        return decision


def image_content_block(deps: "LoopDeps", attachment: MessageAttachment) -> dict[str, Any]:
        data = base64.b64encode(attachment.local_path.read_bytes()).decode("ascii")
        mime_type = attachment.mime_type or "application/octet-stream"
        return deps.transport.image_content_block(mime_type, data)


def build_user_message_content(
        deps: "LoopDeps",
        user_message: str,
        attachments: tuple[MessageAttachment, ...] = (),
        turn_context: str = "",
    ) -> str | list[dict[str, Any]]:
        """The user's message, preceded by this turn's own context.

        ``turn_context`` is everything the turn has to say that is not the
        user's own words: the session checkpoint, retrieved memory, the skills
        this turn activates, the orchestration policy, and the time.  It is
        built here rather than appended to the system prompt because the system
        prompt is the *head* of the request -- the one part a provider's prefix
        cache can serve for every call in the session -- so a per-turn value
        placed there is paid for by invalidating the whole conversation behind
        it.  In the message the same text is appended once and then frozen,
        which is also the honest record of what the model was actually shown.

        The user's own words stay last, so the request is read after the
        material that frames it.
        """
        direct_images: list[MessageAttachment] = []
        fallback_attachments: list[MessageAttachment] = []
        if attachments:
            if deps.supports_vision:
                for attachment in attachments:
                    if attachment.kind == "image" and attachment.local_path.is_file():
                        direct_images.append(attachment)
                    else:
                        fallback_attachments.append(attachment)
            else:
                fallback_attachments = list(attachments)

        parts: list[str] = []
        if turn_context:
            parts.append(turn_context)
        if user_message:
            parts.append(user_message)
        fallback_context = format_attachment_context(fallback_attachments)
        if fallback_context:
            parts.append(fallback_context)
        text = "\n\n".join(parts)
        if not direct_images:
            return text

        blocks: list[dict[str, Any]] = []
        if text.strip():
            blocks.append({"type": "text", "text": text})
        blocks.extend(image_content_block(deps, attachment) for attachment in direct_images)
        return blocks


def plan_orchestration(
        deps: "LoopDeps",
        ctx: AgentContext,
        user_message: str,
    ) -> OrchestrationDecision:
        skill_catalog: Optional[SkillCatalog] = ctx.metadata.get("skill_catalog")
        planner = OrchestrationPlanner.from_skill_catalog(skill_catalog)
        return planner.decide(
            user_message,
            tools_enabled=ctx.tools_enabled,
            has_spawn_agent=bool(deps.registry.tools_with_capability("orchestration")),
        )



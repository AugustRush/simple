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
import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional

from agent import shared
from agent.config import _now
from agent.core.event_domains import LLMResponseParsed
from agent.memory import consolidation
from agent.memory.consolidation import estimate_message_tokens
from agent.memory.system import ContextLimitError
from agent.core.output import _active_event_collector
from agent.core.spawn_planning import promote_orchestration_runs as _promote_into_memory
from agent.memory.system import ContextManager, LTMEntry
from agent.core.attachments import MessageAttachment, format_attachment_context
from agent.core.turn_context import (
    TURN_REQUEST_KEY,
    active_orchestration_runs as _active_orchestration_runs,
    emit_event as _emit_event,
    claim_missing_usage_warning,
    active_agent_id,
    schedule_domain_event,
)
from agent.core.retry import with_llm_retry
from agent.core.llm_errors import is_content_filter_block, merge_continuation_text
from agent.core.payload_shape import describe_payload
from agent.orchestration.planner import OrchestrationDecision, OrchestrationPlanner
from agent.security.content_filter import summarize_tool_result
from agent.usage import extract_provider_usage
from agent.skills.catalog import SkillCatalog

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agent.core.agent import AgentContext

logger = logging.getLogger(__name__)

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

    #: How this agent makes a model call, and its retry policy.  Carried as the
    #: *agent's own* callables rather than as a :class:`ModelCall` and a
    #: :class:`RetryPolicy`, because ``_create`` is a seam thirty-eight tests patch
    #: on the instance: a struct built from the transport would go around the patch
    #: and those tests would silently stop intercepting the model.
    create: Callable[..., Any]
    retry_policy: Any

    #: Content-filter recovery, as a seam on the agent: a test patches it to drive
    #: the recovery path without provoking a real filter block.  The streaming half
    #: of the model call is *not* here -- ``llm_attempt`` reaches it through the
    #: agent directly, so declaring it would be a field nothing in the loop reads.
    recover_from_content_filter: Callable[..., Any]

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
            create=agent._create,
            retry_policy=agent._retry_policy(),
            recover_from_content_filter=agent._recover_from_content_filter,
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


def next_structured_output_budget(
        deps: "LoopDeps",
        ctx: "AgentContext",
        tools: list[dict],
        current_budget: int,
    ) -> int:
        """The larger cap to retry a truncated structured output with.

        Bounded by the room the window actually has, which is the same quantity
        the per-call cap is derived from -- measuring it any other way is how the
        escalation ends up asking for more output than the window can hold.

        The old form reached it through
        ``current_budget + max(0, input_budget - estimate - 256)``, with
        ``input_budget`` shrunk by ``current_budget``.  That expression equals
        the room when the room is the larger of the two and ``current_budget``
        otherwise, so it is *not* the same number: measured on this machine, a
        request leaving 61413 tokens of room under a 64000 cap returned 64000
        there and 61413 here.  What is preserved is the escalation *decision*,
        because the caller retries only when this exceeds ``current_budget`` --
        below that both forms decline, and above it both return the room.
        Writing it as the room removes the dependence on ``current_budget``
        appearing in the budget's reserve, and never names a cap the window
        cannot hold.
        """
        desired = max(current_budget * 2, current_budget + 4096)
        return min(desired, max(1, deps.output_room(ctx, tools)))


def build_summarized_tool_results(
        tool_uses: list[dict],
        results: list[str],
    ) -> str:
        """Build a text summary of tool results for recovery after content filter block."""
        lines = ["[Tool results summarized — original output triggered content filter]\n"]
        for tu, res in zip(tool_uses, results):
            summary = summarize_tool_result(
                tu["name"], res, include_preview=False
            )
            lines.append(f"- {tu['name']}: {str(summary)[:400]}")
        return "\n".join(lines)


async def recover_incomplete_tool_response(
        deps: "LoopDeps",
        ctx: "AgentContext",
        tools: list[dict],
        response: Any,
    ) -> tuple[Any, Optional[str]]:
        """Retry truncated structured output without committing partial protocol."""
        model = deps.effective_model(ctx)
        if not deps.transport.has_incomplete_tool_calls(response, model=model):
            return response, None
        current_budget = int(deps.max_tokens)
        attempts = min(2, max(0, int(deps.max_truncation_continuations)))
        for _ in range(attempts):
            next_budget = next_structured_output_budget(deps, 
                ctx,
                tools,
                current_budget,
            )
            if next_budget <= current_budget:
                break
            current_budget = next_budget
            response = await with_llm_retry(
                deps.retry_policy,
                deps.create,
                ctx,
                tools,
                output_max_tokens=current_budget,
            )
            if not deps.transport.has_incomplete_tool_calls(
                response, model=model
            ):
                return response, None
        return (
            response,
            "Structured tool output remained truncated; no complete tool call was "
            f"committed after retrying with max_tokens={current_budget}. Reduce the "
            "single tool payload or increase the provider context/output budget.",
        )
async def handle_end_turn(
        deps: "LoopDeps",
        ctx: "AgentContext",
        response: Any,
        text: str,
        streamed_text: str,
        prior_text: str,
        tool_result_history: list[tuple[str, str]],
        tools: list[dict],
    ) -> tuple[str, Optional[str], Optional[Any]]:
        """Finalize a non-tool-use response.

        Returns ``(result_text, continuation_error, tool_response)``.  Picks
        the best text (parsed > streamed > prior), promotes a tool's
        ``summary_text`` if the model returned nothing, appends the assistant
        entry, attempts truncation continuation when the transport reports
        one, and falls back to an apology when the response is entirely
        empty.  ``continuation_error`` is non-None only when continuation
        still failed; ``tool_response`` is non-None only when the continuation
        came back asking for tools, in which case the caller must run that
        response through its step loop instead of ending the turn.
        """
        result_text = text or streamed_text or prior_text
        if not result_text and tool_result_history:
            result_text = synthesize_tool_only_response(tool_result_history)
        ctx.messages.append(
            deps.transport.build_final_message(
                response, result_text, model=deps.effective_model(ctx)
            )
        )

        completion_error = response_completion_error(deps, response, ctx)
        if completion_error:
            result_text, continuation_error, tool_response = (
                await continue_truncated_response(deps, 
                    ctx, result_text, tools
                )
            )
            if tool_response is None:
                # Only a continuation that ended in text finalises the
                # truncated message.  One that came back with tool calls has
                # already replaced the history with its own conversation,
                # whose last message is the continue prompt, not this
                # response's entry.
                ctx.messages[-1] = deps.transport.build_final_message(
                    response, result_text, model=deps.effective_model(ctx)
                )
            return result_text, continuation_error, tool_response

        # Silent stop with no text and no tool history is almost always a
        # safety-filtered or malformed response; speak up so the user isn't
        # left wondering whether anything happened.
        if not result_text and not tool_result_history:
            result_text = (
                "I received your message but was unable to "
                "generate a response. Please try rephrasing "
                "your request or checking if it triggered a "
                "content policy filter."
            )
        return result_text, None, None


async def continue_truncated_response(
        deps: "LoopDeps",
        ctx: "AgentContext",
        partial_text: str,
        tools: list[dict],
    ) -> tuple[str, Optional[str], Optional[Any]]:
        """Try to complete a truncated final response with bounded follow-up calls.

        The continuation carries the turn's own tools.  A continuation sent
        without them cannot produce a native tool call, and a model that wants
        one anyway writes it as text in its own markup -- which is how a garbled
        token-delimiter dump once became a turn's final answer.  A continuation
        that comes back asking for tools is not an answer at all: it is the
        step the truncated response was cut out of, so its conversation is
        adopted into ``ctx`` and its response is returned as the third element
        for the caller's step loop to execute through the normal tool path.
        """
        merged = partial_text
        continuation_ctx = build_continuation_context(ctx)
        attempts = 0
        for _ in range(max(0, int(deps.max_truncation_continuations))):
            attempts += 1
            continuation_ctx.messages.append(
                {"role": "user", "content": CONTINUE_PROMPT}
            )
            response = await deps.create(continuation_ctx, tools)
            stop_reason, text, tool_uses = parse_response(deps, 
                response, continuation_ctx
            )
            if stop_reason == "tool_use" and tool_uses:
                # The calls were issued against the continuation's message
                # list -- the continue prompt, and whatever compaction the
                # continuation's own call had to do -- so that list, not the
                # pre-continuation one, is the history the tool results must
                # attach to for the next request to read coherently.
                ctx.messages[:] = continuation_ctx.messages
                return merged, None, response
            merged = merge_continuation_text(merged, text)
            continuation_error = response_completion_error(deps, 
                response, continuation_ctx
            )
            if continuation_error is None:
                return merged, None, None
            continuation_ctx.messages.append(
                deps.transport.build_final_message(
                    response, text, model=deps.effective_model(continuation_ctx)
                )
            )
        return (
            merged,
            f"Model response remained truncated after "
            f"{attempts} auto-continue attempts",
            None,
        )


def synthesize_tool_only_response(
        tool_history: list[tuple[str, str]]
    ) -> str:
        """Promote a successful tool's own ``summary_text`` to the turn reply.

        ``tool_history`` stores ``(tool_name, summary_text_or_empty)`` per
        call (the parsing happens at append time so this list stays small).
        The most recent non-empty summary wins.  Used when the model
        produced only tool calls and no closing text — without this, the
        user would see silence.
        """
        for _tool_name, summary in reversed(tool_history):
            if summary:
                return summary
        return ""


def build_continuation_context(ctx: "AgentContext") -> "AgentContext":
        """Create the minimal context needed for bounded auto-continue requests.

        ``_provider_step``, ``turn_id`` and ``_orchestration_child`` ride along
        so the continuation's provider call is attributed to the step and turn
        it belongs to rather than being logged as a fresh foreground step one;
        the turn-request reference rides along so an emergency compaction
        inside the continuation cannot evict the request the turn exists to
        answer.
        """
        # Imported here rather than at module scope: ``AgentContext`` lives in
        # ``agent/core/agent.py``, which imports this module, so a module-level
        # import would be a cycle.  The class is nine fields and its only runtime
        # use in this file is this constructor.  Moving it down instead would mean
        # moving ``DEFAULT_SYSTEM_PROMPT`` out of ``agent/__init__.py`` as well,
        # because the field default reads it -- a larger change than the cycle it
        # would remove.
        from agent.core.agent import AgentContext

        return AgentContext(
            agent_id=ctx.agent_id,
            role=ctx.role,
            messages=list(ctx.messages),
            system_prompt=ctx.system_prompt,
            tools_enabled=ctx.tools_enabled,
            metadata={
                key: ctx.metadata[key]
                for key in (
                    "model_override",
                    "context_manager",
                    "session_id",
                    "turn_id",
                    "_provider_step",
                    "_orchestration_child",
                    TURN_REQUEST_KEY,
                )
                if key in ctx.metadata
            },
        )


def parse_response(
        deps: "LoopDeps", response: Any, ctx: Optional["AgentContext"] = None
    ) -> tuple[str, str, list[dict]]:
        stop_reason, text, tool_uses = deps.transport.parse_response(
            response, model=deps.effective_model(ctx) if ctx else None
        )
        # Announced from the parser because this is where the normalised vocabulary
        # exists; see ``LLMResponseParsed``.  Fire-and-forget: this is a sync method
        # on the hot path, and the bus delivers to listeners that must not be able
        # to change the turn.
        if deps.llm_event_bus.has_listeners(LLMResponseParsed):
            schedule_domain_event(
                deps.llm_event_bus,
                LLMResponseParsed,
                agent_id=active_agent_id(),
                stop_reason=stop_reason,
                tool_uses=len(tool_uses or ()),
            )
        return stop_reason, text, tool_uses


def response_completion_error(
        deps: "LoopDeps", response: Any, ctx: Optional["AgentContext"] = None
    ) -> Optional[str]:
        return deps.transport.completion_error(
            response, model=deps.effective_model(ctx) if ctx else None
        )


CONTINUE_PROMPT = (
        "Continue exactly from where you left off. "
        "Do not repeat previous text. "
        "Do not restart the answer."
    )
def compact_body_for_turn(
        deps: "LoopDeps", ctx: "AgentContext"
    ) -> None:
        """Cut the body to a low-water mark once, before the turn's first call.

        Compaction rewrites the front of the body, and the front is the one
        thing a provider's prefix cache can reuse.  Doing it here rather than
        inside the provider loop is what keeps the rest of a turn append-only:
        a turn that cuts mid-flight both loses context the model was actively
        using and pays for a fresh prefix on every step that follows, whereas a
        single cut at the boundary leaves every step an append.

        `_prepare_provider_context` keeps its own call as an emergency fallback
        for a step whose own tool results overflow the window.  That is rare
        precisely because this cut leaves half the budget free, and it stays
        because the alternative is a hard failure in the middle of a turn.
        """
        tools = ctx.metadata.get("_selected_tools")
        if not isinstance(tools, list):
            return
        budget = deps.input_token_budget(ctx, tools)
        if budget <= 0:
            return
        messages_before = len(ctx.messages)
        protected = deps.protected_turn_messages(ctx)
        context_manager = deps.context_manager_for(ctx)
        compact = getattr(context_manager, "compact_messages", None)
        if callable(compact):
            ctx.messages = compact(
                ctx.messages, input_token_budget=budget, protected=protected
            )
        else:
            # A manager is optional here, exactly as it is in
            # `_prepare_provider_context`: fitting a request into the window is
            # a property of the request rather than of memory, so
            # `fit_to_budget` is the same algorithm without the instance.  Runs
            # with no manager (the scheduler's `stateless` policy) and callers
            # that supply a retrieval-only stand-in both land here.
            ctx.messages = ContextManager.fit_to_budget(
                ctx.messages,
                input_token_budget=budget,
                estimate_tokens=estimate_message_tokens,
                protected=protected,
            )
        if len(ctx.messages) != messages_before:
            # Read and cleared by the first `_prepare_provider_context` of the
            # turn, so the recorded row says "this call paid full price for its
            # history" only on the call that actually did.
            ctx.metadata["_body_compacted_pending"] = True


def promote_turn_orchestration_runs(deps: "LoopDeps") -> None:
        """Promote only bounded, successful run summaries into session memory.

        See :func:`agent.core.spawn_planning.promote_orchestration_runs`; the
        store, clock and entry class are supplied here rather than imported
        there, so the promotion can be tested without any of them.
        """
        _promote_into_memory(
            deps.context_manager_for(),
            _active_orchestration_runs.get() or [],
            now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            new_id=shared._new_id,
            entry_type=LTMEntry,
        )


async def stream_response(
        deps: "LoopDeps",
        ctx: "AgentContext",
        tools: list[dict],
        callback: Callable[[str], Any],
        reasoning_callback: Optional[Callable[[str], Any]] = None,
    ) -> tuple[Any, str]:
        """Stream response and return (full_response, collected_text).

        ``callback`` may be a plain sync function or an async coroutine
        function; the transport handles both.  ``reasoning_callback`` is the
        turn's sink channel for the model's thinking — resolved by the caller
        (see ``send_message``), which needs it under the same emission
        tracking as the answer, since a replay would double either.

        Sub-agents never reach this method, because they are never given a
        stream callback.  That is also what keeps their thinking out of the
        parent's note: several run at once, and their fragments interleaved
        would read as one confused train of thought.
        """
        prepare_provider_context(deps, ctx, tools)
        response, text = await deps.transport.stream(
            model=deps.effective_model(ctx),
            # Derived per call by `_prepare_provider_context`: a request that
            # nearly fills the window has to leave itself less room for the
            # answer than a short one does.  Falls back to the configured cap
            # when that method took an early return.
            max_tokens=int(ctx.metadata.pop("_output_cap", deps.max_tokens)),
            system=ctx.system_prompt,
            messages=ctx.messages,
            tools=tools,
            callback=callback,
            reasoning_callback=reasoning_callback,
        )
        observe_provider_usage(deps, ctx, response)
        return response, text


async def recover_from_content_filter(
        deps: "LoopDeps",
        ctx: "AgentContext",
        tool_uses: list[dict] | None,
        results: list[str] | None,
    ) -> Any | None:
        """Attempt recovery after a content filter rejection.

        Strategy: roll back the last round of messages from ctx.messages,
        replace the offending tool results with safe summaries, and retry
        the API call once. If it still fails, return None.
        """
        # Learn from the actual rejection only when attribution is unambiguous.
        # A provider rejection is request-level; labeling every result in a
        # multi-tool batch as risky poisons unrelated outputs.
        if tool_uses and results:
            if len(results) == 1:
                await deps.content_filter.learn_and_persist([results[0]])
            else:
                logger.info(
                    "Content filter recovery skipped classifier learning for "
                    "ambiguous multi-tool batch of %d results",
                    len(results),
                )

        # Roll back: remove the last assistant message and its tool results.
        # The last message(s) in ctx.messages are the tool_result entries,
        # preceded by the assistant tool_use message.
        messages_before = len(ctx.messages)
        dropped_roles: list[str] = []
        if tool_uses:
            # The transport knows how many trailing tool-result messages it
            # appended for this batch; +1 for the preceding assistant turn.  The
            # count comes from the batch itself, not from how many calls were
            # requested: a short `results` appends fewer messages, and cutting
            # by call count would take one message of real history with it.
            result_msg_count = deps.transport.tool_result_rollback_count(
                tool_uses, results, model=deps.effective_model(ctx)
            )
            cut = result_msg_count + 1
            dropped_roles = [
                str(message.get("role", "?")) for message in ctx.messages[-cut:]
            ]
            del ctx.messages[-cut:]

        # Append a summarized user message instead
        if tool_uses and results:
            summary = build_summarized_tool_results(tool_uses, results)
            ctx.messages.append({"role": "user", "content": summary})
            logger.info(
                "Content filter recovery: rolled back %d messages, "
                "replaced with summarized tool results",
                len(dropped_roles),
            )

        if dropped_roles:
            # This is the one path that rewrites context the model has already
            # seen.  Without an event here the turn's event stream and
            # ctx.messages disagree, and "never retrieved" becomes
            # indistinguishable from "retrieved, then rolled back".
            _emit_event(
                "context_rolled_back",
                reason="content_filter",
                messages_before=messages_before,
                messages_after=len(ctx.messages),
                messages_dropped=len(dropped_roles),
                dropped_roles=",".join(dropped_roles),
                tool_names=",".join(tu["name"] for tu in (tool_uses or [])),
                replaced_with="summary" if (tool_uses and results) else "nothing",
            )

        # Retry once with the cleaned messages
        try:
            tools = list(ctx.metadata.get("_selected_tools") or [])
            return await deps.create(ctx, tools)
        except Exception as retry_exc:
            if is_content_filter_block(retry_exc):
                logger.warning(
                    "Content filter recovery retry also blocked — "
                    "summarized results still triggered filter"
                )
            else:
                logger.warning(
                    "Content filter recovery retry failed: %s", retry_exc
                )
            return None
def prepare_provider_context(
        deps: "LoopDeps",
        ctx: "AgentContext",
        tools: list[dict],
        *,
        output_max_tokens: int | None = None,
    ) -> None:
        budget = deps.input_token_budget(
            ctx,
            tools,
            output_max_tokens=output_max_tokens,
        )
        ctx.metadata["_last_input_token_budget"] = budget
        if budget <= 0:
            raise ContextLimitError("provider input token budget is not positive")
        context_manager = deps.context_manager_for(ctx)
        # Compaction normally runs once, at the turn boundary (see
        # `_compact_body_for_turn`), so that everything after it is an append.
        # What remains here is the emergency path: a step whose own tool results
        # have overflowed the window.  It stays because the alternative is a
        # hard failure mid-turn, and it is rare because the boundary cut leaves
        # half the budget free.
        messages_before = len(ctx.messages)
        # This is the cut an interjection can arm: it runs mid-turn, after any
        # interjection has been appended as a real user message, so "the newest
        # message that looks like a request" would name the interjection and free
        # the turn's own request for eviction.  Passing the object is what makes
        # the proxy unnecessary here.
        protected = deps.protected_turn_messages(ctx)
        if context_manager is not None:
            ctx.messages = context_manager.compact_messages(
                ctx.messages, input_token_budget=budget, protected=protected
            )
        else:
            # Fitting the request into the window is not a memory operation: it
            # drops the oldest turns of *this* payload and reads or writes
            # nothing durable.  So a turn with no manager -- the scheduler's
            # `stateless` policy nulls it to keep runs independent -- still has
            # to be able to compact.  Without this, such a run raised
            # ContextLimitError the moment its own transcript outgrew the
            # window, where the interactive path would simply have dropped
            # turns and carried on.
            ctx.messages = ContextManager.fit_to_budget(
                ctx.messages,
                input_token_budget=budget,
                estimate_tokens=estimate_message_tokens,
                protected=protected,
            )
        # Whether this call is about to pay full price for its history: the body
        # it sends is not the one the previous call sent plus an append.  A cut
        # made at the turn boundary belongs to the turn's *first* call, which is
        # the one that pays for it; the flag is cleared here so the steps after
        # it are not all reported as misses.  The in-loop cut is counted
        # directly, because it happened on this call.
        ctx.metadata["_payload_shape"] = describe_payload(
            ctx.system_prompt,
            tools,
            ctx.messages,
            compacted=bool(ctx.metadata.pop("_body_compacted_pending", False))
            or len(ctx.messages) != messages_before,
        )
        estimate = deps.token_budget.estimate_input_tokens(ctx.messages, ctx=ctx)
        if estimate >= budget:
            raise ContextLimitError(
                f"provider input estimate {estimate} is not below budget {budget}"
            )
        # Record what this payload was predicted to cost so the provider's exact
        # count can calibrate the estimator once the call returns.  The provider
        # charges for system prompt and tool schemas too, so the comparable
        # prediction is messages + overhead.
        configured_cap = deps.configured_output_cap(output_max_tokens)
        overhead = deps.token_budget.payload_overhead(ctx, tools)
        ctx.metadata["_predicted_input_tokens"] = estimate + max(0, overhead)
        # What the two halves of the prediction cost, for the split-aware
        # calibration in `_observe_provider_usage`: the head at the price this
        # budget actually charged it, the body at the raw heuristic so the
        # learned correction never feeds on itself.
        ctx.metadata["_predicted_head_cost"] = max(0, overhead)
        ctx.metadata["_predicted_body_raw"] = deps.token_budget.estimate_raw_body_tokens(ctx)
        # The cap actually sent, derived here because this is the only place the
        # real input estimate exists.  `max_tokens` is not tokenised into the
        # prompt, so it costs nothing in cache terms to vary it per call -- and
        # varying it is what lets the configured cap stay generous: on a short
        # request the window has room for all of it, and only a request that
        # nearly fills the window has to settle for less.  Deriving it any other
        # way (shrinking the cap itself) is how long answers get truncated.
        ctx.metadata["_output_cap"] = max(
            1, min(configured_cap, deps.output_room(ctx, tools))
        )


def observe_provider_usage(
        deps: "LoopDeps", ctx: "AgentContext", response: Any
    ) -> None:
        """Feed the provider's exact input count back into the estimator.

        Without this the character heuristic never learns: it carries the same
        systematic bias for the life of the process, and the only signal that it
        was wrong is a provider-side failure.

        This is also the one place a provider request's real cost is known, so
        it is where the row that makes the prompt cache measurable is written.
        `agent.core.payload_shape` says which fields the row carries and why
        those are the ones that explain a miss.
        """
        predicted = int(ctx.metadata.pop("_predicted_input_tokens", 0) or 0)
        head_cost = int(ctx.metadata.pop("_predicted_head_cost", 0) or 0)
        body_raw = int(ctx.metadata.pop("_predicted_body_raw", 0) or 0)
        shape = ctx.metadata.pop("_payload_shape", None)
        observed_usage = getattr(deps.transport, "observed_usage", None)
        usage = (
            observed_usage(response)
            if callable(observed_usage)
            else extract_provider_usage(response)
        )
        context_manager = deps.context_manager_for(ctx)
        if context_manager is None:
            return
        if predicted > 0 and usage.input_tokens > 0:
            # Like for like: `predicted` is the whole payload (messages plus the
            # system prompt and tool schemas), so it must be compared against the
            # provider's whole prompt count.  Feeding it the *uncached* slice
            # instead makes the measurement shrink as caching improves, the
            # estimate always looks too high, and the calibration clamp drags it
            # to its floor -- an estimator that can never learn it under-counts.
            #
            # The split-aware path prices the two halves from their own
            # evidence: the body factor from the difference between consecutive
            # observations (which cancels the head out entirely), the head from
            # the residual, locked per fingerprint.  One shared factor could not
            # price both -- measured on this machine the schemas run ~0.5x the
            # heuristic while ASCII tool transcripts run ~1.4x -- and letting
            # body evidence move a byte-identical head's price was what shrank
            # the input budget mid-turn and cut context the payload still fit.
            head_fp = (
                shape.get("head_fingerprint")
                if isinstance(shape, dict)
                else None
            )
            consolidation = getattr(context_manager, "consolidation", None)
            observe_split = getattr(consolidation, "observe_payload_usage", None)
            if callable(observe_split) and head_fp:
                with shared._suppress_with_log("token calibration skipped"):
                    observe_split(
                        fingerprint=head_fp,
                        head_estimate=head_cost,
                        body_raw=body_raw,
                        actual=usage.input_tokens,
                    )
            elif callable(
                getattr(consolidation, "observe_actual_usage", None)
            ):
                with shared._suppress_with_log("token calibration skipped"):
                    consolidation.observe_actual_usage(
                        predicted, usage.input_tokens
                    )
        if usage.total_tokens <= 0:
            # Nothing worth recording: a row of zeros reads as a request that
            # cost nothing rather than as one whose cost was never reported, and
            # it would drag down every aggregate it entered.  Said out loud
            # once, because the alternative is what this path used to do --
            # leave every streamed turn missing from `usage_events` with
            # nothing anywhere to say why.
            warn_missing_usage(deps, ctx)
            return
        session_id = str(
            ctx.metadata.get("session_id")
            or getattr(getattr(context_manager, "staging", None), "session_id", "")
            or ctx.agent_id
        )
        step = max(1, int(ctx.metadata.get("_provider_step", 1) or 1))
        phase = "subagent" if ctx.metadata.get("_orchestration_child") else (
            "foreground" if step == 1 else "tool_step"
        )
        store = getattr(context_manager, "store", None)
        record = getattr(store, "append_usage_event", None)
        if callable(record):
            metadata: dict[str, Any] = {
                "step": step,
                "predicted_input_tokens": predicted,
                # The ceiling this call was fitted to.  Recorded beside the
                # payload's shape because "did the request fit, or was it cut"
                # is the question the two answer together.
                "input_budget": int(
                    ctx.metadata.get("_last_input_token_budget") or 0
                ),
            }
            if isinstance(shape, dict):
                metadata.update(shape)
            with shared._suppress_with_log("provider usage persistence skipped"):
                record(
                    session_id=session_id,
                    turn_id=str(ctx.metadata.get("turn_id") or ""),
                    phase=phase,
                    model=deps.effective_model(ctx),
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cached_input_tokens=usage.cached_input_tokens,
                    metadata=metadata,
                )
        _emit_event(
            "provider_usage",
            phase=phase,
            step=step,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
        )


def warn_missing_usage(deps: "LoopDeps", ctx: "AgentContext") -> None:
        """Say once that a provider reported no usage for a call.

        Silence here is what let the interactive path go unmeasured: the row was
        skipped, nothing was logged, and the only symptom was an absent column
        in a database nobody was reading.  One line per process is enough to
        turn that into a diagnosable state, and low enough volume to leave on.
        """
        if not claim_missing_usage_warning():
            return
        shared.CONSOLE.print(
            "[yellow]Provider reported no token usage for a call "
            f"({deps.effective_model(ctx)}); it will not appear in "
            "usage_events. If the gateway rejects stream_options, set "
            "providers.<name>.stream_usage to false.[/yellow]"
        )

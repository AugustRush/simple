from __future__ import annotations

import asyncio
import base64
import contextvars
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import html
import inspect
import json
import logging
from pathlib import Path
import time
from typing import Any, Awaitable, Callable, Optional, TYPE_CHECKING

import agent as agent_module
from agent import shared
from agent.config import _compose_system_prompt, _now
from agent.core.agent_settings import AgentSettings
from agent.core.spawn_planning import (
    PlannerDeps,
    SpawnPlanner,
    promote_orchestration_runs,
)
from agent.core.subagent_finalize import (
    build_result_payload,
    finalize_sub_agent,
)
from agent.core.subagent_prepare import prepare_sub_agent
from agent.core.subagent_run import run_with_retries
from agent.core.subagent_factory import (
    SubAgentSpec,
    build_sub_agent,
    settings_from,
)
from agent.core.subagent_context import (
    canonicalize_write_scope,
    compose_sub_system_prompt,
    propagate_sub_metadata,
)
from agent.core.output_contract import (
    normalize_output_contract,
    output_contract_path_resolver,
    string_list,
    validate_output_contract,
    with_output_contract,
)
from agent.core.turn_helpers import protected_turn_messages
from agent.core.bounded_text import (
    append_named_block,
    bounded_json,
    bounded_structured,
    bounded_text,
)
from agent.core.context_assembler import ContextAssembler
from agent.core.events import TOOL_EVENTS
from agent.core.token_budget import TokenBudget
from agent.core.tool_watchdog import ToolLoopOutcome, ToolLoopWatchdog
from agent.core.llm_errors import (
    is_account_exhausted,
    is_content_filter_block,
    is_llm_retryable,
    merge_continuation_text,
    retryable_llm_classes,
)
from agent.core.payload_shape import describe_payload, head_fingerprint
from agent.core.attachments import MessageAttachment, format_attachment_context
from agent.core.output import CliOutputSink, _active_event_collector, _active_sink
from agent.core.event_domains import (
    TurnFinished,
    TurnStarted,
    LLMRequestStarted,
    LLMResponseParsed,
    LLMResponseReceived,
    LLMRetryScheduled,
)
from agent.core.events import LLM_EVENTS, TURN_EVENTS
from agent.core.llm_attempt import obtain_response
from agent.core.tool_batch import run_tool_batch
from agent.core.tool_loop_decision import handle_stuck_loop
from agent.core.stream_tracking import (
    StreamEmissionTracker as _StreamEmissionTracker,
)
from agent.memory.consolidation import estimate_message_tokens
from agent.memory.system import ContextLimitError, ContextManager, LTMEntry
from agent.orchestration.runtime import (
    RendezvousDirective,
    SubtaskResult,
    SubtaskSpec,
    run_parallel_subtasks as _run_parallel_subtasks,
    run_pipeline_subtasks as _run_pipeline_subtasks,
    run_rendezvous_round as _run_rendezvous_round,
    validate_subtask_specs,
    CAPABILITY_PROFILES,
)
from agent.orchestration import contracts
from agent.orchestration.planner import OrchestrationDecision, OrchestrationPlanner
from agent.pathing import canonicalize_user_path, resolve_workspace_path
from agent.plugins.catalog import PluginCatalog
from agent.runtime.heartbeat import HeartbeatWriter, TurnHeartbeat
from agent.core.interjections import inject_pending_interjections
from agent.core.turn_context import (
    TURN_IN_PROGRESS_KEY as _TURN_IN_PROGRESS_KEY,
)
from agent.core.turn_context import (
    TURN_REQUEST_KEY as _TURN_REQUEST_KEY,
    missing_usage_warned as _missing_usage_warned,
    active_agent_context as _active_agent_context,
    active_orchestration_runs as _active_orchestration_runs,
    emit_event as _emit_event,
    active_agent_id as _active_agent_id,
    emit_domain_event as _emit_domain_event,
    schedule_domain_event as _schedule_domain_event,
)
from agent.core.model_call import ModelCall, create_turn_response
from agent.core.retry import RetryPolicy, with_llm_retry
from agent.core.turn_loop import (
    LoopDeps,
    assistant_message,
    build_summarized_tool_results,
    compact_body_for_turn,
    handle_end_turn,
    observe_provider_usage,
    parse_response,
    prepare_provider_context,
    prepare_turn,
    promote_turn_orchestration_runs,
    recover_incomplete_tool_response,
    recover_from_content_filter,
    run_turn_loop,
    stream_response,
    tool_result_messages,
)
from agent.kernel.service import resolve
from agent.tools.executor import RegularToolExecutor
from agent.skills.catalog import SkillCatalog
from agent.tools.runtime import ToolRegistry, ToolView
from agent.usage import extract_provider_usage
from agent.security.content_filter import (
    ContentFilter,
    default_model_path,
    filter_tool_results,
    summarize_tool_result,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from agent.core.transport import ModelEndpoint, ModelTransport

DEFAULT_SYSTEM_PROMPT = agent_module.DEFAULT_SYSTEM_PROMPT
logger = logging.getLogger(__name__)

_UPSTREAM_SUMMARY_BUDGET = 2400
_UPSTREAM_DETAIL_BUDGET = 2400
_UPSTREAM_STRUCTURED_BUDGET = 1200
_RENDEZVOUS_INPUT_BUDGET = 6000

#: Set on ``ctx.metadata`` for the duration of one turn, and read by
#: ``BaseAgent._post_turn_maintenance``.  That method performs the only two
#: edits to a provider payload that a prefix cache cannot survive -- compaction
#: cuts the front of the body, and ``_with_task_context`` rewrites the head --
#: and both are correct *between* turns but ruinous inside one, because a turn's
#: steps are meant to share a prefix.  Module-level rather than a class
#: attribute because the reader is a ``staticmethod``.

#: The message object ``_prepare_turn`` appended for this turn, held so
#: compaction can recognise it by identity.  A cut inside a turn must not remove
#: what the turn was asked to do, and the only reliable way to name that message
#: is to keep the object: the alternative -- "the newest message that looks like
#: a user request" -- is falsified by an interjection, which is a real user
#: message appended *after* the request (see ``agent.core.interjections``).

# Context variable so built-in tools can access the active AgentContext


class _TurnLoopState:
    """One turn's step-loop state.

    A plain object the caller owns and passes in, rather than attributes on
    ``self``: ``send_message`` may run several turns concurrently on one agent
    instance, and ``self`` is shared between them.  Storing this state on the
    instance let one turn read another's ``trace_status`` and
    ``pending_tool_response``, which a concurrent-session test caught.
    """

    __slots__ = (
        "pending_tool_response",
        "result_text",
        "trace_status",
        "trace_error",
        "steps",
    )

    def __init__(self) -> None:
        self.pending_tool_response: Any = None
        self.result_text = ""
        self.trace_status = "ok"
        self.trace_error: Optional[str] = None
        self.steps = 0



from agent.core.turn_context import interaction_log as _interaction_log  # noqa: E402
from agent.core.turn_context import preview_text as _preview_text  # noqa: E402
from agent.core.turn_context import trace_latency as _trace_latency  # noqa: E402










async def _execute_regular_tool_calls(
    regular_calls: list[tuple[int, dict]],
    executor: Any,
) -> list[str]:
    """Run a batch of non-orchestration tool calls, preserving call order.

    Batches containing a ``shell`` call run sequentially: a medium-risk
    shell command blocks on the human consent menu, and other tools must
    not execute or print while that menu is open (parallel results and
    heartbeats corrupt the prompt).  Pure parallel batches keep the fast
    ``asyncio.gather`` path.
    """
    results: list[str] = [""] * len(regular_calls)
    if any(tu.get("name") == "shell" for _idx, tu in regular_calls):
        for position, (_idx, tu) in enumerate(regular_calls):
            try:
                results[position] = await executor.run(tu)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                results[position] = json.dumps(
                    {"ok": False, "error": f"tool '{tu['name']}' raised: {exc}"}
                )
        return results
    raw = await asyncio.gather(
        *[executor.run(tu) for _idx, tu in regular_calls],
        return_exceptions=True,
    )
    for position, (_idx, _tu) in enumerate(regular_calls):
        outcome = raw[position]
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
        if isinstance(outcome, Exception):
            results[position] = json.dumps(
                {"ok": False, "error": f"tool '{_tu['name']}' raised: {outcome}"}
            )
        elif isinstance(outcome, BaseException):
            raise outcome
        else:
            results[position] = outcome
    return results


@dataclass
class AgentContext:
    """State for a single agent instance."""

    agent_id: str = field(default_factory=shared._new_id)
    role: str = "assistant"
    messages: list[dict] = field(default_factory=list)
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    tools_enabled: bool = True
    metadata: dict = field(default_factory=dict)


@dataclass
class AgentResult:
    agent_id: str
    content: str
    tool_calls_made: list[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class SubAgentProgressEvent:
    kind: str
    role: Optional[str] = None
    task: Optional[str] = None
    message: str = ""
    completed: int = 0
    total: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _TaskLocalContextStack:
    """Deprecated: kept only as a no-op shim for any third-party caller.

    The runtime no longer maintains a context stack — ``_active_agent_context``
    (a ContextVar) is the single source of truth for the current
    AgentContext.  Sub-agents run in their own BaseAgent instance with
    their own ContextVar, so a per-agent stack was always degenerate
    (at most one item).
    """

    def __bool__(self) -> bool:
        return False


_ = _TaskLocalContextStack  # publicly importable for any third-party caller




@dataclass
class _ContentFilterRecovery:
    """The rollback state a provider content-filter block needs.

    A provider can reject a request because of what a *tool result* contained.
    Recovering means un-sending the last protocol unit and resending it with
    the offending output summarized.  That requires remembering what was last
    submitted, and carrying the recovered response into the next loop
    iteration so it is consumed instead of issuing a fresh LLM call.

    That is three pieces of state with one lifecycle.  They used to be three
    loose locals threaded through a 500-line function, cleared at four
    separate sites — where forgetting one clears the rollback point and turns
    a recoverable block into a failed turn.  Naming the state machine makes
    the invariant ("submission recorded ⇒ rollback possible") checkable.
    """

    submitted_tool_uses: list[dict] | None = None
    submitted_results: list[str] | None = None
    pending_response: Any | None = None

    def record_submission(
        self, tool_uses: list[dict], results: list[str]
    ) -> None:
        """Note what just went to the provider, so it can be rolled back."""
        self.submitted_tool_uses = list(tool_uses)
        self.submitted_results = list(results)

    def forget_submission(self) -> None:
        """Drop the rollback point — nothing recoverable is outstanding."""
        self.submitted_tool_uses = None
        self.submitted_results = None

    def take_pending_response(self) -> Any | None:
        """Consume a recovered response exactly once."""
        response, self.pending_response = self.pending_response, None
        return response


class BaseAgent:
    """Core agent: streams Claude, handles tool_use loop."""

    _TOOL_LOOP_REPEAT_THRESHOLD = 3
    _TOOL_LOOP_UNPRODUCTIVE_THRESHOLD = 4
    #: Set once, so a provider that never reports usage says so once per
    #: process rather than on every turn of every session.

    def __init__(
        self,
        client: Any,
        registry: ToolRegistry,
        model: str = shared.DEFAULT_MODEL,
        max_tokens: int = shared.DEFAULT_MAX_TOKENS,
        api_format: str = "anthropic",
        supports_vision: bool = False,
        context_window: int | None = None,
        output_reserve: int = shared.DEFAULT_OUTPUT_RESERVE,
        transport: Optional["ModelTransport"] = None,
        services: Any = None,
        orchestration_policy_skill_id: str = "multi-agent-orchestration",
        event_bus: Any = None,
        llm_event_bus: Any = None,
        tool_event_bus: Any = None,
        settings: Any = None,
    ):
        #: The names this class resolves rather than receives.  One name today --
        #: ``llm`` -- and deliberately **not** the whole composition: the caller passes
        #: what this class looks up, so a plugin can build it from its own context
        #: without handing the agent a mapping it could reach anything through.  A
        #: mapping that is too wide is how "which services does this need" stops being a
        #: question anybody can answer.
        self._services = services
        self.client = client
        self.registry = registry
        self.api_format = api_format
        self.supports_vision = supports_vision
        self.model = model
        self.max_tokens = max_tokens
        # Room the input budget must leave free for the answer.  Separate from
        # `max_tokens` on purpose: the cap says how long one response may be,
        # the reserve says how much of the window the input may not take.  See
        # `_input_token_budget`.
        # The tool-loop stuck-detection state machine (agent/core/tool_watchdog.py).
        # One per agent: the thresholds are configuration; the per-turn counters
        # live in the state dict the turn loop owns.
        #: Where this agent's turn and model-call events go.  The module-level
        #: buses, so a listener mounted by a plugin reaches every agent, including
        #: the scheduler's own.  Settable for a test that wants a private one.
        # The composition's buses when it provides them, the process defaults otherwise.
        #
        # Defaulted rather than required because a bus is only *observable*: an agent built
        # directly -- by a test, by an embedder -- has nobody to publish to, and demanding
        # one would be ceremony.  A composition that provides them gets isolation: two
        # runtimes in one process stop sharing a listener set, which is a bug this suite
        # has already had once (a leaked gate from one test winning another test's
        # ``emit_blocking``).
        self._event_bus = event_bus if event_bus is not None else TURN_EVENTS
        self._llm_event_bus = (
            llm_event_bus if llm_event_bus is not None else LLM_EVENTS
        )
        self._tool_event_bus = (
            tool_event_bus if tool_event_bus is not None else TOOL_EVENTS
        )
        self._tool_watchdog = ToolLoopWatchdog(
            repeat_threshold=self._TOOL_LOOP_REPEAT_THRESHOLD,
            unproductive_threshold=self._TOOL_LOOP_UNPRODUCTIVE_THRESHOLD,
        )
        self.output_reserve = output_reserve
        self.context_window = (
            context_window
            if context_window is not None
            else shared.DEFAULT_CONTEXT_WINDOW
        )
        # The transport is the only place provider dispatch happens, so an
        # agent that must route per-call model overrides — anything built
        # from a multi-provider config — receives its RoutingTransport here
        # rather than having one assigned after construction.  A post-hoc
        # assignment is invisible to everything the constructor derives,
        # sub-agents included, which is how a sub-agent came to hold a bare
        # active-provider transport while running another provider's model.
        if transport is None:
            from agent.core.transport import build_transport
            transport = build_transport(api_format, client)
        # The composition's ``llm`` wins when it named one; the argument is what an
        # agent built outside a composition uses.  That precedence is the seam Phase 2
        # asked for -- "a composition can choose the adapter without editing the code
        # that builds the agent" -- and it is why `services` is one name rather than the
        # mapping: the narrower it is, the more the question "what does this need" has an
        # answer.
        if services is not None:
            named = resolve(services, "llm", None)
            if named is not None:
                transport = named
        self._transport: "ModelTransport" = transport
        self.context_assembler = ContextAssembler()
        self.context_manager: Optional[ContextManager] = None
        self.plugin_catalog: Optional["PluginCatalog"] = None
        #: Which skill bundle decides how a turn is orchestrated.  The built-in one
        #: unless a composition names another -- see the planner for why a plugin's
        #: bundle has to be named in full.
        # Received, not poured in afterwards.  `AgentPlugin` used to assign twelve
        # attributes onto the object it had just built, which made "what does this agent
        # need" unanswerable from its signature -- the same complaint as a plugin handed the
        # whole component mapping.  The derivation lives in `AgentSettings.from_config`;
        # this takes the result.
        self.settings = settings if settings is not None else AgentSettings()
        self.orchestration_policy_skill_id = (
            orchestration_policy_skill_id or self.settings.orchestration_policy_skill_id
        )
        self.max_parallel_agents = self.settings.max_parallel_agents

        # Spawn-batch planning (agent/core/spawn_planning.py).  Assigned
        # before anything can call it; helpers are handed over as bound
        # methods so a caller cannot tell the move happened.
        self._spawn_planner = SpawnPlanner(
            PlannerDeps(
                settings=self,
                max_parallel_agents=self.max_parallel_agents,
                string_list=self._string_list,
                normalize_output_contract=self._normalize_output_contract,
                mapping_dict=self._mapping_dict,
                bounded_text=self._bounded_text,
                bounded_structured=self._bounded_structured,
                result_content_limit=self._result_content_limit,
            )
        )
        self.sub_agent_timeout_seconds = self.settings.sub_agent_timeout_seconds
        self.sub_agent_retries = self.settings.sub_agent_retries
        # 0 = derive from max_parallel_agents (see _max_agents_per_turn).
        self.max_agents_per_turn = self.settings.max_agents_per_turn
        self.max_tool_call_iterations = self.settings.max_tool_call_iterations
        self.max_truncation_continuations = self.settings.max_truncation_continuations
        self.max_rendezvous_rounds = 2  # Mirrors OrchestrationDecision default.
        self.result_content_max_chars = self.settings.result_content_max_chars
        self.llm_max_retries = self.settings.llm_max_retries
        self.llm_retry_base_delay = self.settings.llm_retry_base_delay
        self.workspace_root: Optional[Path] = None
        self._base_system_prompt: str = ""
        self.content_filter: ContentFilter = ContentFilter.load(default_model_path())
        self._content_filter_threshold: float = 0.7



    def _emit_subagent_event(self, event: SubAgentProgressEvent) -> None:
        sink = _active_sink.get()
        if sink is not None:
            sink.on_subagent_event(event)
            return
        CliOutputSink(shared.CONSOLE).on_subagent_event(event)

    @staticmethod
    def _format_role_list(roles: list[str], *, limit: int = 3) -> str:
        cleaned = [str(role).strip() for role in roles if str(role).strip()]
        if not cleaned:
            return "sub-agents"
        if len(cleaned) <= limit:
            return ", ".join(cleaned)
        remaining = len(cleaned) - limit
        return f"{', '.join(cleaned[:limit])}, +{remaining} more"

    def _runtime_progress_message(self, kind: str, payload: dict[str, Any]) -> str:
        mode = str(payload.get("execution_mode", "") or "").strip().lower()
        if kind == "batch_progress" and mode == "parallel":
            completed = int(payload.get("completed", 0) or 0)
            total = int(payload.get("total", 0) or 0)
            running = max(0, total - completed)
            return (
                f"Parallel batch running: {completed}/{total} completed, "
                f"{running} still running"
            )

        if kind == "phase_started" and mode == "pipeline":
            stage_index = int(payload.get("phase_index", 0) or 0)
            ready_count = int(payload.get("ready_count", 0) or 0)
            roles = self._format_role_list(list(payload.get("ready_roles", [])))
            return (
                f"Pipeline stage {stage_index} started: "
                f"{ready_count} ready ({roles})"
            )

        if kind == "phase_finished" and mode == "pipeline":
            stage_index = int(payload.get("phase_index", 0) or 0)
            succeeded = int(payload.get("succeeded_count", 0) or 0)
            failed = int(payload.get("failed_count", 0) or 0)
            if failed:
                return (
                    f"Pipeline stage {stage_index} finished: "
                    f"{succeeded} succeeded, {failed} failed"
                )
            return f"Pipeline stage {stage_index} finished: {succeeded} succeeded"

        if kind == "phase_note" and mode == "pipeline":
            if payload.get("phase_kind") == "skipped":
                skipped = len(payload.get("skipped_ids") or [])
                roles = self._format_role_list(list(payload.get("skipped_roles", [])))
                reason = str(payload.get("reason", "") or "")
                suffix = f" ({reason})" if reason else ""
                return f"Pipeline: {skipped} blocked agents skipped{suffix}: {roles}"

        if kind == "phase_started" and mode == "rendezvous":
            round_index = int(payload.get("phase_index", 0) or 0)
            round_total = int(payload.get("phase_total", 0) or 0)
            participants = int(payload.get("participant_count", 0) or 0)
            roles = self._format_role_list(list(payload.get("participant_roles", [])))
            return (
                f"Debate round {round_index}/{round_total} started: "
                f"{participants} participants ({roles})"
            )

        if kind == "phase_finished" and mode == "rendezvous":
            round_index = int(payload.get("phase_index", 0) or 0)
            round_total = int(payload.get("phase_total", 0) or 0)
            succeeded = int(payload.get("succeeded_count", 0) or 0)
            failed = int(payload.get("failed_count", 0) or 0)
            if failed:
                return (
                    f"Debate round {round_index}/{round_total} finished: "
                    f"{succeeded} succeeded, {failed} failed"
                )
            return (
                f"Debate round {round_index}/{round_total} finished: "
                f"{succeeded} views collected"
            )

        if kind == "phase_note" and mode == "rendezvous":
            round_index = int(payload.get("phase_index", 0) or 0)
            round_total = int(payload.get("phase_total", 0) or 0)
            continue_count = int(payload.get("continue_count", 0) or 0)
            if bool(payload.get("final")):
                return (
                    f"Lead synthesis complete after round "
                    f"{round_index}/{round_total}"
                )
            if bool(payload.get("stop")):
                return (
                    f"Lead summary after round {round_index}/{round_total}: "
                    "no further round needed"
                )
            roles = self._format_role_list(list(payload.get("continue_roles", [])))
            next_round = min(round_total, round_index + 1)
            return (
                f"Lead summary ready for round {next_round}/{round_total}: "
                f"{continue_count} continue ({roles})"
            )

        return ""

    def _emit_runtime_progress_event(self, kind: str, payload: dict[str, Any]) -> None:
        self._emit_subagent_event(
            SubAgentProgressEvent(
                kind=kind,
                completed=int(payload.get("completed", 0) or 0),
                total=int(payload.get("total", 0) or 0),
                message=self._runtime_progress_message(kind, payload),
                metrics=dict(payload),
            )
        )

    def current_context(self) -> Optional["AgentContext"]:
        return _active_agent_context.get()

    def _context_manager_for(
        self,
        ctx: Optional["AgentContext"] = None,
    ) -> Optional[ContextManager]:
        request_ctx = ctx or self.current_context()
        if request_ctx is not None:
            scoped = request_ctx.metadata.get("context_manager")
            if scoped is not None:
                return scoped
        return self.context_manager

    def _effective_model(self, ctx: Optional["AgentContext"] = None) -> str:
        request_ctx = ctx or self.current_context()
        override = (
            request_ctx.metadata.get("model_override") if request_ctx else None
        )
        if override is None:
            return self.model
        if not isinstance(override, str):
            raise TypeError("model override must be a nonblank string or None")
        if not override.strip():
            raise ValueError("model override must not be blank")
        return override

    def endpoint_for(self, model: Optional[str] = None) -> "ModelEndpoint":
        """The client — and the wire format — that owns *model*.

        Public because the background LLM consumers (memory consolidation, the
        session-end flush, the evolution engine) need a client *for a model*
        and used to reach for whichever client the process happened to hold.
        Routing the lookup through the agent's transport is what stops them
        pairing another group's model with the active provider's client.
        """
        return self._transport.endpoint_for(model)

    def consolidation_model(self, cfg: dict) -> str:
        """The model this agent's memory consolidation runs on.

        ``context.consolidation.model`` may name any configured group's model
        — the config docs suggest a cheaper one, and the cheapest model is
        often in another group.  Every caller that starts a background
        consolidation reads the model from here, so the worker, the
        session-end flush and the manual tidy cannot disagree about it.
        """
        consolidation = (cfg.get("context") or {}).get("consolidation") or {}
        configured = consolidation.get("model")
        return str(configured or getattr(self, "model", "") or "")

    def consolidation_endpoint(self, cfg: dict) -> "ModelEndpoint":
        """The endpoint that owns the model ``consolidation_model`` returned."""
        return self.endpoint_for(self.consolidation_model(cfg))


    async def _execute_subtask_spec(self, spec: SubtaskSpec) -> SubtaskResult:
        # Call the single execution primitive directly — no JSON round-trip
        # through the tool registry.
        payload = await self._execute_agent(
            role=spec.role,
            task=spec.task,
            system_suffix=spec.system_suffix,
            expected_output=spec.expected_output,
            output_contract=dict(spec.output_contract) if spec.output_contract else None,
            write_scope=list(spec.write_scope) if spec.write_scope else None,
            capability_profile=spec.capability_profile,
            handoff=dict(spec.handoff) if spec.handoff else None,
            run_id=spec.run_id,
        )
        content = str(
            payload.get("content")
            or payload.get("partial_content")
            or ""
        )
        structured_content = payload.get("structured_content")
        full_content = str(payload.get("full_content") or "")
        return SubtaskResult(
            id=spec.id,
            ok=bool(payload.get("ok")),
            content=content,
            summary=self._summarize_subtask_result(content),
            structured_content=structured_content,
            full_content=full_content,
            tool_calls_made=list(payload.get("tool_calls_made", [])),
            error=payload.get("error"),
            artifact_ref=str(payload.get("artifact_ref") or ""),
        )

    @staticmethod
    def _summarize_subtask_result(content: str, limit: int = 400) -> str:
        """See :meth:`agent.core.spawn_planning.SpawnPlanner.summarize_subtask_result`."""
        return SpawnPlanner.summarize_subtask_result(content, limit)

    @staticmethod
    def _append_named_block(
        base_text: str,
        heading: str,
        lines: list[str],
    ) -> str:
        """See :func:`agent.core.bounded_text.append_named_block`."""
        return append_named_block(base_text, heading, lines)

    @staticmethod
    def _bounded_json(value: Any, limit: int = 2400) -> str:
        """See :func:`agent.core.bounded_text.bounded_json`."""
        return bounded_json(value, limit)

    @staticmethod
    def _bounded_text(value: Any, limit: int) -> str:
        """See :func:`agent.core.bounded_text.bounded_text`."""
        return bounded_text(value, limit)

    def _result_content_limit(self) -> int:
        return min(
            shared.MAX_RESULT_CONTENT_CHARS,
            max(
                shared.MIN_RESULT_CONTENT_CHARS,
                int(self.result_content_max_chars or 0),
            ),
        )

    @classmethod
    def _bounded_structured(cls, value: Any, limit: int = 2400) -> Any:
        """See :func:`agent.core.bounded_text.bounded_structured`."""
        return bounded_structured(value, limit)

    def _with_upstream_summaries(
        self,
        spec: SubtaskSpec,
        upstream_summaries: dict[str, str],
        upstream_results: dict[str, SubtaskResult] | None = None,
    ) -> SubtaskSpec:
        summary_text = self._bounded_text(
            "\n".join(
                f"- {dep}: {summary}" for dep, summary in upstream_summaries.items()
            ),
            _UPSTREAM_SUMMARY_BUDGET,
        )
        task = self._append_named_block(
            spec.task,
            "Upstream summaries:",
            [summary_text] if summary_text else [],
        )
        handoff: dict[str, Any] = dict(spec.handoff)
        structured_lines: list[str] = []
        detail_lines: list[str] = []
        for dep, result in (upstream_results or {}).items():
            if result.structured_content is not None:
                bounded = self._bounded_structured(result.structured_content)
                handoff[dep] = bounded
                structured_lines.append(f"- {dep}: {self._bounded_json(bounded)}")
            if result.content and len(result.content) > len(result.summary):
                detail_lines.append(f"- {dep}: {result.content}")
        detail_text = self._bounded_text(
            "\n".join(detail_lines),
            _UPSTREAM_DETAIL_BUDGET,
        )
        task = self._append_named_block(
            task,
            "Upstream detail:",
            [detail_text] if detail_text else [],
        )
        structured_text = self._bounded_text(
            "\n".join(structured_lines),
            _UPSTREAM_STRUCTURED_BUDGET,
        )
        task = self._append_named_block(
            task,
            "Upstream structured results:",
            [structured_text] if structured_text else [],
        )
        return replace(spec, task=task, handoff=handoff)

    def _with_lead_summary(
        self,
        spec: SubtaskSpec,
        lead_summary: str,
        lead_structured_context: dict[str, Any] | None = None,
    ) -> SubtaskSpec:
        if not lead_summary and not lead_structured_context:
            return spec
        task = self._append_named_block(
            spec.task,
            "Lead summary:",
            [self._bounded_text(lead_summary, _UPSTREAM_SUMMARY_BUDGET)]
            if lead_summary
            else [],
        )
        structured_lines = []
        for dep, value in (lead_structured_context or {}).items():
            bounded = self._bounded_structured(value)
            structured_lines.append(f"- {dep}: {self._bounded_json(bounded)}")
        task = self._append_named_block(
            task,
            "Lead structured results:",
            [
                self._bounded_text(
                    "\n".join(structured_lines),
                    _UPSTREAM_STRUCTURED_BUDGET,
                )
            ]
            if structured_lines
            else [],
        )
        handoff: dict[str, Any] = dict(spec.handoff)
        for dep, value in (lead_structured_context or {}).items():
            handoff[dep] = self._bounded_structured(value)
        return replace(spec, task=task, handoff=handoff)

    async def _summarize_rendezvous_round(
        self, results: list[SubtaskResult]
    ) -> str | RendezvousDirective:
        """Synthesize a round of multi-agent results via parent LLM reasoning.

        Falls back to the original string concatenation when the client is
        unavailable (e.g. in tests) or the API call fails.
        """
        # Build the synthesis prompt
        lines: list[str] = []
        for result in results:
            status = "ok" if result.ok else "error"
            detail = self._bounded_text(
                result.summary or result.content or result.error or "",
                800,
            )
            lines.append(
                f"Agent [{result.id}] (status={status}): {detail}"
            )
        results_text = self._bounded_text(
            "\n".join(f"- {line.rstrip()}" for line in lines),
            _RENDEZVOUS_INPUT_BUDGET,
        )
        prompt = (
            f"You are coordinating a multi-agent debate. Below are the results "
            f"from the current round. Your job: synthesize the perspectives, "
            f"identify points of agreement and disagreement, and decide whether "
            f"another round is needed.\n\n"
            f"## Round Results\n{results_text}\n\n"
            f"## Instructions\n"
            f"Respond with a JSON object (no markdown, no code fences):\n"
            f'{{"summary": "synthesis of all perspectives", '
            f'"stop": true_or_false, '
            f'"continue_with": ["agent_id", ...] or null}}\n\n'
            f"- summary: synthesize the key insights, conflicts, and consensus\n"
            f"- stop: JSON boolean (true or false, never a string) — true if consensus "
            f"is clear or further rounds would not add value\n"
            f"- continue_with: null to keep all agents, or a list of agent IDs to "
            f"select specific agents for the next round"
        )
        with shared._suppress_with_log("rendezvous LLM synthesis failed; using string-concat fallback"):
            resp_text = await self._call_llm(
                prompt,
                system="You are a precise synthesis coordinator. Always respond with valid JSON.",
                max_tokens=1024,
            )
            if resp_text:
                directive = self._parse_rendezvous_directive(
                    resp_text,
                    known_ids={result.id for result in results},
                )
                if directive is not None:
                    return directive
        # Fallback: original string concatenation
        summary_lines = []
        for result in results:
            status = "ok" if result.ok else "error"
            detail = result.summary or result.error or ""
            summary_lines.append(
                f"- {result.id} ({status}): {detail}".rstrip()
            )
        return RendezvousDirective(
            summary="\n".join(summary_lines),
            summary_quality="concatenation",
        )

    @staticmethod
    def _parse_rendezvous_directive(
        resp_text: str,
        *,
        known_ids: set[str],
    ) -> RendezvousDirective | None:
        """See :meth:`agent.core.spawn_planning.SpawnPlanner.parse_rendezvous_directive`."""
        return SpawnPlanner.parse_rendezvous_directive(resp_text, known_ids=known_ids)

    @staticmethod
    def _mapping_dict(value: Any) -> dict[str, Any]:
        return contracts.mapping_dict(value)

    @staticmethod
    def _normalize_output_contract(output_contract: dict[str, Any] | None) -> dict[str, Any]:
        """See :func:`agent.core.output_contract.normalize_output_contract`."""
        return normalize_output_contract(output_contract)

    @classmethod
    def _with_output_contract(
        cls,
        task: str,
        expected_output: str,
        output_contract: dict[str, Any] | None,
    ) -> str:
        """See :func:`agent.core.output_contract.with_output_contract`."""
        return with_output_contract(task, expected_output, output_contract)

    def _resolve_output_contract_path(self, raw_path: str) -> Path:
        """Kept because it is the resolver the validator used to call directly."""
        return self._output_contract_path_resolver()(raw_path)

    def _validate_output_contract(
        self,
        content: str,
        *,
        expected_output: str,
        output_contract: dict[str, Any] | None,
    ) -> tuple[bool, str, dict[str, Any] | None, str | None]:
        """See :func:`agent.core.output_contract.validate_output_contract`.

        The path resolver is built here from this agent's registry context and
        workspace root, which is the dependency that used to be read inside the
        validator.
        """
        return validate_output_contract(
            content,
            expected_output=expected_output,
            output_contract=output_contract,
            resolve_path=self._output_contract_path_resolver(),
        )

    def _output_contract_path_resolver(self):
        """A resolver bound to this agent's output dir and workspace root."""
        output_dir_str = self.registry.get_context("output_dir")
        return output_contract_path_resolver(
            output_dir=Path(output_dir_str) if output_dir_str else None,
            workspace_root=self.workspace_root,
        )

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        """See :func:`agent.core.output_contract.string_list`."""
        return string_list(value)

    @staticmethod
    def _looks_like_implementation_work(role: str, task: str) -> bool:
        return SpawnPlanner.looks_like_implementation_work(role, task)

    def _default_capability_profile(
        self,
        role: str,
        task: str,
        explicit: str,
        *,
        orchestration_mode: str,
    ) -> str:
        return self._spawn_planner.default_capability_profile(
            role, task, explicit, orchestration_mode=orchestration_mode
        )

    def _spawn_tool_use_to_spec(
        self,
        tool_use: dict[str, Any],
        *,
        index: int,
        orchestration_decision: OrchestrationDecision,
    ) -> SubtaskSpec:
        """See :meth:`agent.core.spawn_planning.SpawnPlanner.spawn_tool_use_to_spec`."""
        return self._spawn_planner.spawn_tool_use_to_spec(
            tool_use, index=index, orchestration_decision=orchestration_decision
        )

    def _max_agents_per_turn(self) -> int:
        """Total sub-agents one turn may run, independent of concurrency."""
        return self._spawn_planner.max_agents_per_turn()

    def _spawn_result_payload(
        self,
        spec: SubtaskSpec,
        result: SubtaskResult,
        *,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """See :meth:`agent.core.spawn_planning.SpawnPlanner.spawn_result_payload`."""
        return self._spawn_planner.spawn_result_payload(spec, result, extra=extra)

    async def _run_orchestrated_spawn_calls(
        self,
        spawn_calls: list[tuple[int, dict]],
        orchestration_decision: OrchestrationDecision,
    ) -> tuple[list[str], dict[str, Any]]:
        specs: list[SubtaskSpec] = []
        for index, (_result_index, tool_use) in enumerate(spawn_calls, start=1):
            spec = self._spawn_tool_use_to_spec(
                tool_use,
                index=index,
                orchestration_decision=orchestration_decision,
            )
            specs.append(spec)

        run_id = shared._new_id()
        specs = [replace(spec, run_id=run_id) for spec in specs]
        # Validate the graph before dispatch for every mode — including the
        # single-spawn "direct" path, which the per-mode runtimes would
        # otherwise skip.
        validate_subtask_specs(specs)
        # max_parallel_agents bounds concurrency, not total work: a single
        # model turn emitting N spawn calls runs all N to completion.  Cap the
        # batch so one turn cannot fan out unboundedly, and fail loudly so the
        # model can replan into fewer, broader subtasks.
        agent_budget = self._max_agents_per_turn()
        if len(specs) > agent_budget:
            raise ValueError(
                f"too many sub-agents in one turn: {len(specs)} requested but the "
                f"limit is {agent_budget}; group the work into fewer, broader "
                "subtasks or run them across separate turns"
            )

        execution_mode = self._derive_execution_mode_from_spawn_calls(spawn_calls)
        telemetry: dict[str, Any] = {
            "execution_mode": execution_mode,
            "spec_count": len(specs),
        }

        if execution_mode == "parallel":
            executed = await self._call_with_optional_telemetry(
                self.run_parallel_subtasks,
                specs,
                telemetry=telemetry,
            )
        elif execution_mode == "pipeline":
            executed = await self._call_with_optional_telemetry(
                self.run_pipeline_subtasks,
                specs,
                telemetry=telemetry,
            )
        elif execution_mode == "rendezvous":
            rendezvous_results = await self._call_with_optional_telemetry(
                self.run_rendezvous_subtasks,
                specs,
                max_rounds=orchestration_decision.max_rendezvous_rounds,
                telemetry=telemetry,
            )
            latest_by_id = {result.id: result for result in rendezvous_results}
            executed = [latest_by_id[spec.id] for spec in specs if spec.id in latest_by_id]
        elif execution_mode == "direct":
            spec = specs[0]
            result = await self._execute_subtask_spec(spec)
            executed = [result]
        else:
            executed = []

        results_by_id = {result.id: result for result in executed}
        active_runs = _active_orchestration_runs.get()
        if active_runs is not None:
            active_runs.append({"run_id": run_id, "results": list(executed)})
        # A rendezvous run's deliverable is the lead synthesis, not the raw
        # final-round positions.  Attach it to the first payload (all tool
        # results for a turn reach the model in one block, so the rest only
        # need a pointer).
        synthesis = str(telemetry.get("lead_summary") or "").strip()
        synthesis_attached = False
        missing_reason = (
            "skipped because an upstream pipeline stage failed"
            if execution_mode == "pipeline"
            else f"no result produced by the {execution_mode} run"
        )
        payloads: list[str] = []
        for spec in specs:
            result = results_by_id.get(spec.id)
            if result is None:
                payloads.append(
                    json.dumps(
                        {
                            "ok": False,
                            "role": spec.role,
                            "task": spec.task,
                            "error": missing_reason,
                        },
                        ensure_ascii=False,
                    )
                )
                continue
            extra: dict[str, Any] | None = None
            if synthesis:
                if not synthesis_attached:
                    extra = {
                        "run_synthesis": self._bounded_text(synthesis, 4000),
                        "run_synthesis_quality": str(
                            telemetry.get("summary_quality") or ""
                        ),
                    }
                    synthesis_attached = True
                else:
                    extra = {
                        "run_synthesis": (
                            "see run_synthesis on the first result of this run"
                        )
                    }
            payloads.append(self._spawn_result_payload(spec, result, extra=extra))
        return payloads, telemetry

    def _derive_execution_mode_from_spawn_calls(
        self,
        spawn_calls: list[tuple[int, dict]],
    ) -> str:
        """See :meth:`agent.core.spawn_planning.SpawnPlanner.execution_mode_from_spawn_calls`."""
        return self._spawn_planner.execution_mode_from_spawn_calls(spawn_calls)

    @staticmethod
    async def _call_with_optional_telemetry(
        runner: Callable[..., Awaitable[Any]],
        *args: Any,
        telemetry: dict[str, Any],
        **kwargs: Any,
    ) -> Any:
        if "telemetry" in inspect.signature(runner).parameters:
            return await runner(*args, telemetry=telemetry, **kwargs)
        return await runner(*args, **kwargs)

    def _canonicalize_write_scope(self, raw_scope: str) -> Path:
        """See :func:`agent.core.subagent_context.canonicalize_write_scope`."""
        output_dir_str = self.registry.get_context("output_dir")
        return canonicalize_write_scope(
            raw_scope,
            workspace_root=self.workspace_root,
            output_dir=Path(output_dir_str) if output_dir_str else None,
        )

    async def run_parallel_subtasks(
        self,
        specs: list[SubtaskSpec],
        *,
        max_concurrency: int | None = None,
        telemetry: dict[str, Any] | None = None,
    ) -> list[SubtaskResult]:
        return await _run_parallel_subtasks(
            specs,
            executor=self._execute_subtask_spec,
            max_concurrency=max_concurrency or self.max_parallel_agents,
            canonicalize_write_scope=self._canonicalize_write_scope,
            telemetry=telemetry,
            progress_callback=self._emit_runtime_progress_event,
        )

    async def run_pipeline_subtasks(
        self,
        specs: list[SubtaskSpec],
        *,
        telemetry: dict[str, Any] | None = None,
    ) -> list[SubtaskResult]:
        async def _executor(
            spec: SubtaskSpec,
            upstream_summaries: dict[str, str],
            *,
            upstream_results: dict[str, SubtaskResult] | None = None,
        ) -> SubtaskResult:
            return await self._execute_subtask_spec(
                self._with_upstream_summaries(
                    spec,
                    upstream_summaries,
                    upstream_results,
                )
            )

        return await _run_pipeline_subtasks(
            specs,
            executor=_executor,
            max_concurrency=self.max_parallel_agents,
            canonicalize_write_scope=self._canonicalize_write_scope,
            telemetry=telemetry,
            progress_callback=self._emit_runtime_progress_event,
        )

    async def run_rendezvous_subtasks(
        self,
        specs: list[SubtaskSpec],
        *,
        max_rounds: int = 2,
        telemetry: dict[str, Any] | None = None,
    ) -> list[SubtaskResult]:
        async def _executor(
            spec: SubtaskSpec,
            *,
            round_index: int,
            lead_summary: str,
            lead_structured_context: dict[str, Any] | None = None,
        ) -> SubtaskResult:
            adjusted = (
                self._with_lead_summary(
                    spec,
                    lead_summary,
                    lead_structured_context,
                )
                if round_index > 1
                else spec
            )
            # Scope per-round LTM observations so repeated rounds of the same
            # agent do not collide under one run id.
            if adjusted.run_id:
                adjusted = replace(
                    adjusted,
                    run_id=f"{adjusted.run_id}#r{round_index}",
                )
            return await self._execute_subtask_spec(adjusted)

        return await _run_rendezvous_round(
            specs,
            executor=_executor,
            summarize=self._summarize_rendezvous_round,
            max_rounds=max_rounds,
            max_concurrency=self.max_parallel_agents,
            canonicalize_write_scope=self._canonicalize_write_scope,
            telemetry=telemetry,
            progress_callback=self._emit_runtime_progress_event,
        )

    # ── Format-aware API helpers ──────────────────────────────────────────

    @property
    def _token_budget(self) -> TokenBudget:
        """The window arithmetic, built on first use.

        Lazy rather than assigned in ``__init__`` for two reasons.  A subclass
        that skips ``__init__`` (there is one in the tests, which wants the
        arithmetic and nothing else) still gets a working object.  And the
        budget reads ``context_window`` / ``output_reserve`` live, so a caller
        that resizes the window after construction does not have to remember to
        rebuild this.
        """
        budget = getattr(self, "_token_budget_instance", None)
        if budget is None:
            budget = TokenBudget(
                # Both resolved on use rather than captured: a subclass that
                # skips __init__ has neither, and only the path that needs one
                # should require it.
                context_manager_for=lambda ctx: self._context_manager_for(ctx),
                assembler_for=lambda: self.context_assembler,
                settings=self,
            )
            self._token_budget_instance = budget
        return budget

    def _configured_output_cap(self, output_max_tokens: int | None) -> int:
        """See :meth:`agent.core.token_budget.TokenBudget.configured_output_cap`."""
        return self._token_budget.configured_output_cap(output_max_tokens)



    def _payload_overhead(self, ctx: "AgentContext", tools: list[dict]) -> int:
        """See :meth:`agent.core.token_budget.TokenBudget.payload_overhead`."""
        return self._token_budget.payload_overhead(ctx, tools)

    def _output_room(self, ctx: "AgentContext", tools: list[dict]) -> int:
        """See :meth:`agent.core.token_budget.TokenBudget.output_room`."""
        return self._token_budget.output_room(ctx, tools)

    def _input_token_budget(
        self,
        ctx: "AgentContext",
        tools: list[dict],
        *,
        output_max_tokens: int | None = None,
    ) -> int:
        """See :meth:`agent.core.token_budget.TokenBudget.input_token_budget`."""
        return self._token_budget.input_token_budget(
            ctx, tools, output_max_tokens=output_max_tokens
        )

    def _estimate_input_tokens(
        self,
        messages: list[dict],
        *,
        ctx: Optional["AgentContext"] = None,
    ) -> int:
        """See :meth:`agent.core.token_budget.TokenBudget.estimate_input_tokens`."""
        return self._token_budget.estimate_input_tokens(messages, ctx=ctx)

    def _estimate_raw_body_tokens(self, ctx: "AgentContext") -> int:
        """See :meth:`agent.core.token_budget.TokenBudget.estimate_raw_body_tokens`."""
        return self._token_budget.estimate_raw_body_tokens(ctx)

    @staticmethod
    def _protected_turn_messages(ctx: "AgentContext") -> list[dict]:
        """The messages a cut running inside this turn must keep.

        The turn's own request, and nothing else: history is evictable, the
        reason the turn exists is not.  Empty outside a turn, where the caller
        has no object to hand over and ``ContextManager._protected_indexes``
        falls back to the newest message that looks like a request.
        """
        # Moved to `agent.core.turn_helpers` so the turn loop can reach it without an
        # agent.  Kept here because callers hold an agent.
        return protected_turn_messages(ctx)





    async def _stream_response(
        self,
        ctx: "AgentContext",
        tools: list[dict],
        callback: Any,
        reasoning_callback: Any = None,
    ) -> Any:
        """Streaming model call.  A facade: see :meth:`_create` for why.

        ``agent/core/llm_attempt.py`` calls it and four tests patch it, so the name
        has to stay reachable on the instance.  The logic is in
        ``agent/core/turn_loop.py``.
        """
        return await stream_response(
            self._loop_deps(), ctx, tools, callback, reasoning_callback
        )

    async def _recover_from_content_filter(
        self, ctx: "AgentContext", tool_uses: list, results: list
    ) -> Any:
        """Content-filter recovery.  A facade: see :meth:`_create` for why.

        A test replaces it to drive the recovery path without provoking a real
        filter block, so the name has to stay reachable on the instance.
        """
        return await recover_from_content_filter(
            self._loop_deps(), ctx, tool_uses, results
        )

    def _prepare_provider_context(
        self,
        ctx: "AgentContext",
        tools: list[dict],
        *,
        output_max_tokens: int | None = None,
    ) -> None:
        """Fit the request into the window.  A facade: see :meth:`_create` for why.

        The logic is in ``agent/core/turn_loop.py``.  The name stays reachable on the
        instance because the tests drive the two-step protocol directly --
        ``_prepare_provider_context`` then ``_observe_provider_usage`` -- and they read
        as steps of a turn rather than as calls into the loop module.
        """
        return prepare_provider_context(
            self._loop_deps(), ctx, tools, output_max_tokens=output_max_tokens
        )

    def _observe_provider_usage(self, ctx: "AgentContext", response: Any) -> None:
        """Fold a response's token usage back into the estimator.  A facade.

        See :meth:`_create` for why the name stays; the logic is in
        ``agent/core/turn_loop.py``.
        """
        return observe_provider_usage(self._loop_deps(), ctx, response)

    async def _create(
        self,
        ctx: "AgentContext",
        tools: list[dict],
        *,
        output_max_tokens: int | None = None,
    ) -> Any:
        """Non-streaming model call.  A **facade**, not a second implementation.

        The logic lives in :func:`agent.core.model_call.create_turn_response`, and
        this method exists because ``_create`` is a seam the suite genuinely uses:
        thirty-eight tests replace it to stand in for the provider, and they are
        right to -- "how this agent calls the model" is a thing a test should be able
        to answer without building a transport.

        One line of delegation cannot drift.  What must not happen is a *second*
        implementation appearing here, so a guard asserts this body is exactly a
        return of ``create_turn_response``.
        """
        return await create_turn_response(
            self._model_call(), ctx, tools, output_max_tokens=output_max_tokens
        )

    def _loop_deps(self) -> LoopDeps:
        """This agent's turn-loop dependency surface.

        A facade like :meth:`_create`, and for the same reason: the tests drive the
        loop's own helpers directly -- ``prepare_turn``, the token budgets -- and
        building the surface is the agent's job, not theirs.
        """
        return LoopDeps.of(self)

    def _model_call(self) -> ModelCall:
        """This agent's model-call surface, in the shape the call leg takes.

        The counterpart of :meth:`_retry_policy`, and for the same reason: the call
        leg is a module function that ``agent/core/llm_attempt.py`` reaches without
        importing ``agent.core.agent``, so it must be handed six members rather than
        an agent.
        """
        # The two callables that fill in the request and fold its usage back now live
        # in ``agent/core/turn_loop.py`` and take that module's dependency surface, so
        # they are bound to one here rather than read off the agent.  A single
        # ``deps`` for the whole model call is also what keeps the request consistent:
        # preparing with one surface and observing with another would let a
        # mid-turn reassignment split the two halves of one call.
        deps = self._loop_deps()
        return ModelCall(
            transport=deps.transport,
            effective_model=deps.effective_model,
            configured_output_cap=deps.configured_output_cap,
            prepare_provider_context=lambda ctx, tools, **kw: prepare_provider_context(
                deps, ctx, tools, **kw
            ),
            observe_provider_usage=lambda ctx, response: observe_provider_usage(
                deps, ctx, response
            ),
            event_bus=deps.llm_event_bus,
        )

    def _retry_policy(self) -> RetryPolicy:
        """This agent's retry policy, in the shape the retry leg takes.

        The policy is an object rather than three arguments because the retry leg is
        a module function now: ``agent/core/llm_attempt.py`` calls it and documents
        that it does not import ``agent.core.agent``, so handing it an agent would
        have undone that.  Three values keep the promise.
        """
        return RetryPolicy(
            max_retries=self.llm_max_retries,
            base_delay=self.llm_retry_base_delay,
            event_bus=self._llm_event_bus,
            is_retryable=self._is_llm_retryable,
        )










    @staticmethod
    def _clear_context_restart_message(
        tool_calls: list[dict],
        results: list[str],
    ) -> str | None:
        """Return a deferred clear-context restart message from a tool batch."""
        for tool_call, raw_result in zip(tool_calls, results):
            if tool_call.get("name") != "clear_context":
                continue
            try:
                payload = json.loads(str(raw_result or ""))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("ok") is False:
                continue
            if payload.get("clear_context_requested") is True:
                restart_message = str(payload.get("restart_message", "")).strip()
                if restart_message:
                    return restart_message
        return None

    def _format_agent_error(self, exc: Exception) -> str:
        if isinstance(exc, ContextLimitError):
            return f"ContextLimitError: {exc}"
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return "Model request timed out"
        if isinstance(exc, ValueError):
            return f"Invalid model request: {exc}"
        text = str(exc)
        # Account-level exhaustion (spent quota, empty balance) reads like a
        # transient 429 but is not: the retry loop already refuses to replay
        # it, and what the user needs is to be told *that*, not the raw body.
        if self._is_account_exhausted(exc):
            # The provider's own body carries the one thing the user cannot
            # guess -- when the window reopens -- so quote it instead of
            # paraphrasing: "It will reset at 2026-09-23 00:03:07 +0800 CST".
            reset_hint = ""
            at = text.lower().find("reset")
            if at != -1:
                clause = text[at:at + 80].split(".")[0].strip()
                reset_hint = f"，配额重置时间：{clause}"
            return (
                "模型服务账户不可用：当前 provider 的配额已用尽或账户已欠费，"
                f"请求没有被发送到模型，重试不会恢复{reset_hint}。"
                "请在配置里换一个仍有额度的 provider/模型，或为该账户充值。"
                f"原始错误：{text}"
            )
        # A model id reached an endpoint that does not serve it.  The raw
        # 400 tells the user which names the endpoint wanted, but not that
        # the fix is a config one -- the model's provider ownership in
        # config.json, or the model picker -- which is the thing to say.
        if "supported api model names" in text.lower() and "you passed" in text.lower():
            return (
                "模型被发送到了不支持它的接口（400）。"
                "这通常是配置问题：检查 config.json 里该模型归属的 provider"
                "（models/default_model），或在模型选择器里换一个当前服务支持的模型。"
                f"原始错误：{text}"
            )
        # The endpoint answered with a web page.  Measured once in this
        # project's run history: 5663 characters of opencode's own "Not Found"
        # page (title `Not Found | opencode`) stored verbatim as the run's
        # error -- which says nothing, because the interesting fact is that the
        # request reached a site and not an API route.  Whose page it is comes
        # from the title, and that is what makes the message usable.
        if text.lstrip().startswith("<") and "<html" in text[:512].lower():
            title = ""
            lowered = text.lower()
            opened = lowered.find("<title")
            if opened != -1:
                start = lowered.find(">", opened)
                end = lowered.find("</title", start + 1) if start != -1 else -1
                if end != -1:
                    title = " ".join(text[start + 1:end].split())[:80]
            named = f"，页面标题：{title}" if title else ""
            return (
                f"模型接口返回的是网页而不是 API 响应（共 {len(text)} 字符{named}）。"
                "这通常说明该 provider 的 base_url 指向了站点首页或一个不存在的路径，"
                "而不是 API 路由：先确认 base_url，再重试。"
                f"响应开头：{text[:200]}"
            )
        return text or exc.__class__.__name__


    @staticmethod
    @staticmethod
    def _is_account_exhausted(exc: Exception) -> bool:
        """See :func:`agent.core.llm_errors.is_account_exhausted`."""
        return is_account_exhausted(exc)

    @staticmethod
    def _retryable_llm_classes() -> tuple[type[BaseException], ...]:
        """See :func:`agent.core.llm_errors.retryable_llm_classes`."""
        return retryable_llm_classes()

    @staticmethod
    def _is_llm_retryable(exc: Exception) -> bool:
        """See :func:`agent.core.llm_errors.is_llm_retryable`.

        Account-level exhaustion is checked first there; that ordering is the
        reason this predicate exists as its own function.
        """
        return is_llm_retryable(exc)

    @staticmethod
    def _is_content_filter_block(exc: Exception) -> bool:
        """See :func:`agent.core.llm_errors.is_content_filter_block`."""
        return is_content_filter_block(exc)




    @staticmethod
    def _extract_summary_text(raw_result: str) -> str:
        """Extract the ``summary_text`` field from a tool result if present.

        Used at tool-batch time so ``tool_result_history`` stores only the
        small synthesizable field per call, not the (potentially MB-sized)
        raw result.
        """
        try:
            payload = json.loads(raw_result)
        except Exception:
            return ""
        if not isinstance(payload, dict) or not payload.get("ok"):
            return ""
        return str(payload.get("summary_text", "")).strip()


    async def _run_tool_uses(
        self,
        tool_uses: list[dict],
        orchestration_decision: OrchestrationDecision | None = None,
    ) -> list[str]:
        # Contract: regular_calls ⊎ spawn_calls partition tool_uses by index,
        # and both branches assign every slot they own before returning.
        # No sentinel — if a slot is unset, that's a programming error caller
        # should see as a missing item, not silently masked.
        results: list[str] = [""] * len(tool_uses)

        def _is_orchestration(tu: dict) -> bool:
            return "orchestration" in self.registry.tool_capabilities(tu["name"])

        regular_calls = [
            (idx, tu) for idx, tu in enumerate(tool_uses) if not _is_orchestration(tu)
        ]
        if regular_calls:
            # The executor publishes tool events; the plugin catalog is one
            # subscriber among possible others.  Passing the shared bus is what
            # keeps a plugin's on_pre_tool gate in force without the tool
            # execution layer naming the plugin machinery.
            regular_executor = RegularToolExecutor(
                self.registry,
                event_bus=self._tool_event_bus,
            )
            regular_results = await _execute_regular_tool_calls(
                regular_calls, regular_executor
            )
            for (idx, _tu), outcome in zip(regular_calls, regular_results):
                results[idx] = outcome

        spawn_calls = [
            (idx, tu) for idx, tu in enumerate(tool_uses) if _is_orchestration(tu)
        ]
        if spawn_calls:
            # All spawn calls go through a single dispatch path via
            # _run_orchestrated_spawn_calls, which routes to the
            # appropriate runtime (parallel, pipeline, rendezvous) or
            # executes directly for a single unorchestrated spawn.
            execution_mode = self._derive_execution_mode_from_spawn_calls(spawn_calls)
            total_spawns = len(spawn_calls)
            roles = ", ".join(tu["input"].get("role", "?") for _, tu in spawn_calls)
            batch_started_at = time.monotonic()
            batch_metrics: dict[str, Any] = {
                "execution_mode": execution_mode,
                "spec_count": total_spawns,
                "max_parallel_agents": self.max_parallel_agents,
            }
            if execution_mode == "rendezvous":
                batch_metrics["max_rounds"] = (
                    orchestration_decision.max_rendezvous_rounds
                    if orchestration_decision is not None
                    else self.max_rendezvous_rounds
                )
            self._emit_subagent_event(
                SubAgentProgressEvent(
                    kind="batch_started",
                    total=total_spawns,
                    message=(
                        f"Starting {total_spawns} sub-agents via {execution_mode} "
                        f"(limit {self.max_parallel_agents}): {roles}"
                    ),
                    metrics=dict(batch_metrics),
                )
            )
            completed = 0
            try:
                try:
                    raw_spawn, runtime_metrics = await self._run_orchestrated_spawn_calls(
                        spawn_calls,
                        orchestration_decision
                        or OrchestrationDecision(mode="explicit"),
                    )
                except ValueError as exc:
                    # The batch was rejected before any child ran (bad graph,
                    # overlapping write scopes, over the per-turn agent cap).
                    # This is a correctable model input error, so report it as
                    # tool results rather than aborting the turn — otherwise the
                    # user loses the whole turn to a mistake the model could fix.
                    batch_metrics["rejected"] = str(exc)
                    raw_spawn = [
                        json.dumps(
                            {
                                "ok": False,
                                "role": str(tu.get("input", {}).get("role", "") or ""),
                                "error": f"sub-agent batch rejected: {exc}",
                            },
                            ensure_ascii=False,
                        )
                        for _, tu in spawn_calls
                    ]
                    runtime_metrics = {}
                completed = len(raw_spawn)
                batch_metrics.update(runtime_metrics)
            finally:
                batch_metrics["completed"] = completed
                batch_metrics["duration_seconds"] = batch_metrics.get(
                    "duration_seconds",
                    max(0.0, time.monotonic() - batch_started_at),
                )
                extra = ""
                if "write_scope_check_seconds" in batch_metrics:
                    extra = (
                        f", scope check {batch_metrics['write_scope_check_seconds']:.4f}s"
                    )
                self._emit_subagent_event(
                    SubAgentProgressEvent(
                        kind="batch_finished",
                        completed=completed,
                        total=total_spawns,
                        message=(
                            f"Sub-agent batch finished via {execution_mode}: "
                            f"{completed}/{total_spawns} completed in "
                            f"{batch_metrics['duration_seconds']:.2f}s{extra}"
                        ),
                        metrics=dict(batch_metrics),
                    )
                )
            for (idx, _tu), outcome in zip(spawn_calls, raw_spawn):
                results[idx] = outcome

        return results

    # ── Tool-loop stuck detection ────────────────────────────────────────



    # ── Tool-loop stuck detection ────────────────────────────────────────

    @staticmethod
    # ── Tool-loop stuck detection ────────────────────────────────────────
    #
    # The detection state machine lives in agent/core/tool_watchdog.py.  These
    # are the names the turn loop and the tests already use.

    @staticmethod
    def _new_watchdog_state() -> dict[str, Any]:
        """See :meth:`agent.core.tool_watchdog.ToolLoopWatchdog.new_state`."""
        return ToolLoopWatchdog.new_state()

    def _check_tool_loop_stuck(
        self,
        state: dict[str, Any],
        tool_uses: list[dict],
        results: list[str],
    ) -> str:
        """See :meth:`agent.core.tool_watchdog.ToolLoopWatchdog.check`."""
        return self._tool_watchdog.check(state, tool_uses, results)

    @staticmethod
    def _stable_tool_loop_key(value: Any) -> str:
        return ToolLoopWatchdog.stable_key(value)

    @staticmethod
    def _tool_use_signature(tool_uses: list[dict]) -> str:
        return ToolLoopWatchdog.tool_use_signature(tool_uses)

    @staticmethod
    def _tool_result_signature(results: list[str]) -> str:
        return ToolLoopWatchdog.tool_result_signature(results)

    @staticmethod
    def _tool_result_looks_unproductive(raw_result: str) -> bool:
        """Kept because tests and callers use it; a one-line reading of
        :meth:`_classify_tool_result`."""
        return ToolLoopWatchdog.classify_tool_result(raw_result).unproductive

    @staticmethod
    def _classify_tool_result(raw_result: str) -> ToolLoopOutcome:
        return ToolLoopWatchdog.classify_tool_result(raw_result)

    @staticmethod
    def _classify_tool_results(results: list[str]) -> ToolLoopOutcome:
        return ToolLoopWatchdog.classify_tool_results(results)

    @staticmethod
    def _tool_result_preview(results: list[str], limit: int = 220) -> str:
        return ToolLoopWatchdog.result_preview(results, limit)

    @classmethod
    def _build_tool_loop_stuck_response(
        cls,
        *,
        user_message: str,
        reason: str,
        tool_uses: list[dict],
        results: list[str],
    ) -> str:
        """See :meth:`agent.core.tool_watchdog.ToolLoopWatchdog.build_stuck_response`."""
        return ToolLoopWatchdog.build_stuck_response(
            user_message=user_message,
            reason=reason,
            tool_uses=tool_uses,
            results=results,
        )





    async def send_message(
        self,
        ctx: "AgentContext",
        user_message: str,
        stream_callback: Optional[Callable[[str], None]] = None,
        attachments: tuple[MessageAttachment, ...] = (),
    ) -> "AgentResult":
        """Run one turn.

        ``stream_callback`` receives the answer as it is produced.  The model's
        thinking, when the provider produces any, goes to the turn's sink
        instead (see ``_stream_response``): it is a sink capability rather than
        a stream the caller asks for, so this signature did not have to grow a
        second callback that every caller would then have to accept.
        """
        tool_calls_made: list[str] = []
        tool_result_history: list[tuple[str, str]] = []
        result_text = ""
        watchdog = self._new_watchdog_state()
        orchestration_token = None
        if not ctx.metadata.get("_orchestration_child"):
            orchestration_token = _active_orchestration_runs.set([])
        filter_recovery = _ContentFilterRecovery()
        turn_started_at = time.perf_counter()
        trace_status = "ok"
        trace_error: str | None = None
        steps = 0
        await _emit_domain_event(
            self._event_bus,
            TurnStarted,
            agent_id=ctx.agent_id,
            tools_enabled=bool(ctx.tools_enabled),
            streamed=bool(stream_callback),
        )
        _interaction_log(
            "turn_started",
            agent_id=ctx.agent_id,
            tools_enabled=ctx.tools_enabled,
            stream=bool(stream_callback),
            message_len=len(user_message),
            message_preview=_preview_text(user_message),
        )
        _trace_latency(
            "send_message_started",
            agent_id=ctx.agent_id,
            tools_enabled=ctx.tools_enabled,
            stream=bool(stream_callback),
            message_len=len(user_message),
        )

        # Set the active agent context so built-in tools (e.g. clear_context)
        # can access the current ctx.messages.  Done before the try so the
        # token is always bound when the finally clause reaches reset().
        _active_ctx_token = _active_agent_context.set(ctx)
        # Publish the cancel token via ContextVar so any tool deep in the
        # call stack (shell subprocess, LLM transport) can register a cleanup
        # without us having to thread the token through every API.
        _cancel_token_token = shared._active_cancel_token.set(
            ctx.metadata.get("cancel_token")
        )
        # The conversation this turn belongs to, for the same reason and for one
        # more: a gateway that asks for "one stable id per conversation" in a
        # request header gets it from here.  Set for the span of the turn by
        # both the interactive paths and the scheduler, whose runs each carry
        # their own `scheduler:<task>:<run>` id, so a scheduled run is one
        # conversation to the provider just as it is to us.
        _session_id_token = shared._active_session_id.set(
            str(ctx.metadata.get("session_id") or "") or None
        )

        # B1: wrap ALL mutations (prompt injection, messages append, stack push)
        # inside the try/finally so they are always cleaned up on error.
        # Start the per-turn heartbeat here so it sees the active sink.
        from agent.core.output import _active_sink as _hb_sink_var

        heartbeat_writer: HeartbeatWriter | None = None
        if ctx.metadata.get("heartbeat_enabled", True) is not False:
            heartbeat_writer = HeartbeatWriter(
                session_id=str(ctx.metadata.get("session_id") or ctx.agent_id),
                agent_id=ctx.agent_id,
                path=ctx.metadata.get("heartbeat_path"),
            )
        heartbeat = TurnHeartbeat(
            writer=heartbeat_writer,
            interval_seconds=float(ctx.metadata.get("heartbeat_interval", 5.0) or 5.0),
            turn_id=str(ctx.metadata.get("turn_id") or ""),
            pending_messages=lambda: len(ctx.metadata.get("pending_messages") or []),
            sink_provider=_hb_sink_var.get,
        )
        await heartbeat.__aenter__()

        state = _TurnLoopState()
        return await run_turn_loop(
            self._loop_deps(),
            self,
            ctx,
            user_message,
            attachments,
            stream_callback,
            max_steps=max(1, int(self.max_tool_call_iterations)),
            orchestration_token=orchestration_token,
            filter_recovery=filter_recovery,
            watchdog=watchdog,
            tool_calls_made=tool_calls_made,
            tool_result_history=tool_result_history,
            heartbeat=heartbeat,
            turn_started_at=turn_started_at,
            active_ctx_token=_active_ctx_token,
            cancel_token_token=_cancel_token_token,
            session_id_token=_session_id_token,
            state=state,
        )

    @staticmethod
    def _post_turn_maintenance(
        *,
        ctx_mgr: Any,
        agent: "BaseAgent",
        ctx: "AgentContext",
        user_content: str,
        assistant_content: str,
        channel: str = "",
        record_kwargs: dict[str, Any] | None = None,
        memory_worker: Any = None,
        system_prompt: str = "",
        task_context: str = "",
        error: str = "",
    ) -> None:
        """Shared turn-post-processing: staging, compaction, consolidation.

        Called by both the CLI interactive loop and the ChannelRunner's
        message handler after every agent turn.  Keeping this in one place
        prevents the two call sites from diverging.
        """
        metadata = getattr(ctx, "metadata", {})
        agent_max_tokens = int(getattr(agent, "max_tokens", 0) or 0)
        default_input_budget = max(
            2048,
            int(getattr(agent, "context_window", 0) or 0) - agent_max_tokens,
        )
        input_token_budget = int(
            metadata.get("_last_input_token_budget")
            or default_input_budget
        )
        # Read here, not at the compaction block below: `metadata` is rebound to
        # the caller's record_kwargs partway through this method, and the mark
        # lives on `ctx.metadata`.  See `_TURN_IN_PROGRESS_KEY`.
        between_turns = not metadata.get(_TURN_IN_PROGRESS_KEY)
        if ctx_mgr:
            consume_suppression = getattr(
                ctx_mgr, "consume_memory_clear_suppression", None
            )
            if callable(consume_suppression) and consume_suppression():
                save_checkpoint = getattr(ctx_mgr, "save_provider_checkpoint", None)
                if callable(save_checkpoint):
                    with shared._suppress_with_log("provider checkpoint save skipped"):
                        checkpoint = save_checkpoint(
                            getattr(ctx, "messages", []),
                            token_budget=max(2048, min(24000, input_token_budget)),
                        )
                        if isinstance(checkpoint, dict) and isinstance(metadata, dict):
                            ctx.metadata["_checkpoint_summary"] = str(
                                checkpoint.get("summary") or ""
                            )
                            ctx.metadata["_has_provider_checkpoint"] = True
                return
            if record_kwargs is None:
                record_kwargs = {}
            # The messages the user sent while this turn was running.  They are
            # part of what this turn answered, so they are journaled with it,
            # between the request and the answer -- see
            # `MemoryStore.write_conversation_exchange`.  Read (not cleared)
            # from metadata: a turn that drained the mailbox twice wrote both
            # batches, and clearing here would drop the second on the floor if
            # this method ever ran twice.
            interjections = (
                metadata.get("_turn_interjections")
                if isinstance(metadata, dict)
                else None
            )
            if not isinstance(interjections, list):
                interjections = []
            record_turn_result = getattr(ctx_mgr, "record_turn_result", None)
            if callable(record_turn_result):
                write_result = record_turn_result(
                    user_content=user_content,
                    assistant_content=assistant_content or "",
                    channel=channel,
                    interjections=interjections,
                    **record_kwargs,
                )
                new_turn = bool(getattr(write_result, "changed", False))
                stage_user = bool(
                    getattr(write_result, "user_created", False)
                    or getattr(write_result, "first_assistant_for_user", False)
                )
                stage_assistant = bool(
                    getattr(write_result, "assistant_created", False)
                )
                # The user's words, in the order they said them: the memory
                # pipeline is built from what the user said, and an interjection
                # is something they said.  Only the rows this call created, so a
                # retried turn does not stage the same text twice.
                stage_interjections = [
                    str(text)
                    for text in getattr(write_result, "interjections_created", ()) or ()
                    if str(text).strip()
                ]
            else:
                fallback_kwargs = dict(record_kwargs)
                fallback_kwargs.pop("assistant_message_id", None)
                recorded = ctx_mgr.record_turn(
                    user_content=user_content,
                    assistant_content=assistant_content or "",
                    channel=channel,
                    **fallback_kwargs,
                )
                new_turn = recorded is not False
                stage_user = new_turn
                stage_assistant = new_turn and bool(assistant_content)
                stage_interjections = []
            record_runtime_event = getattr(ctx_mgr, "record_runtime_event", None)
            if new_turn and callable(record_runtime_event):
                metadata = (
                    record_kwargs.get("metadata", {})
                    if isinstance(record_kwargs, dict)
                    else {}
                )
                if not isinstance(metadata, dict):
                    metadata = {}
                turn_id = str(
                    record_kwargs.get("message_id")
                    or metadata.get("turn_id")
                    or metadata.get("message_id")
                    or ""
                )
                record_runtime_event(
                    "turn_finished",
                    {
                        "user_content": user_content,
                        "assistant_content": assistant_content or "",
                        "error": error or "",
                        "channel": channel,
                        "metadata": record_kwargs,
                    },
                    turn_id=turn_id,
                )
            if new_turn:
                if stage_user:
                    ctx_mgr.staging.append("user", user_content)
                # Interjections are the user's own words, spoken during this
                # turn: the staging buffer is what consolidation reads, so
                # leaving them out would make the memory a record of only the
                # sentences a turn happened to *start* with.
                for text in stage_interjections:
                    ctx_mgr.staging.append("user", text)
                if stage_assistant:
                    ctx_mgr.staging.append("assistant", assistant_content)
                if ctx_mgr.should_enqueue_consolidation():
                    ctx_mgr.enqueue_consolidation("staged_turns")
        # Post-turn compaction serves two purposes:
        # 1. Trim working memory when a session-level context manager
        #    has a lower threshold than the agent default (multi-tenant).
        # 2. Wake the background memory worker so staged content gets
        #    consolidated without delaying the interactive loop.
        # The pre-loop check in send_message handles the common case.
        #
        # Both edits below rewrite a payload that a provider's prefix cache is
        # holding: compaction cuts the front of the body, and `_with_task_context`
        # rebuilds the head.  Each is correct once per *turn*, but a turn issues
        # one provider call per step and those calls are meant to share a prefix,
        # so doing either one mid-turn re-bills the rest of the turn in full.
        # `between_turns` makes "only between turns" a precondition instead of an
        # assumption -- the sole production caller (`complete_turn`) already
        # satisfies it, and a future caller inside a tool loop would otherwise
        # reintroduce the miss silently.  Everything after this block -- turn
        # recording, staging, the checkpoint -- touches no payload and is
        # deliberately not gated.
        if (
            between_turns
            and ctx_mgr
            and ctx_mgr.should_compact_messages(
                ctx.messages, input_token_budget=input_token_budget
            )
        ):
            ctx.messages = ctx_mgr.compact_messages(
                ctx.messages, input_token_budget=input_token_budget
            )
            if system_prompt:
                # Compaction may summarize away the message that carried the
                # original request; keep it reachable via the task context.
                # Same rule as RuntimeSessionState.set_session_prompt — the
                # runtime layer owns the prompt during a turn; maintenance
                # only rebuilds it after compaction, outside any turn.
                ctx.system_prompt = agent_module._with_task_context(
                    system_prompt, task_context
                )
            if (
                memory_worker is not None
                and ctx_mgr.staging.count() >= ctx_mgr.min_messages
            ):
                ctx_mgr.enqueue_consolidation("compact_triggered")
                memory_worker.wake()
        if ctx_mgr:
            save_checkpoint = getattr(ctx_mgr, "save_provider_checkpoint", None)
            if callable(save_checkpoint):
                with shared._suppress_with_log("provider checkpoint save skipped"):
                    checkpoint = save_checkpoint(
                        ctx.messages,
                        token_budget=max(2048, min(24000, input_token_budget)),
                    )
                    if isinstance(checkpoint, dict) and isinstance(metadata, dict):
                        ctx.metadata["_checkpoint_summary"] = str(
                            checkpoint.get("summary") or ""
                        )
                        ctx.metadata["_has_provider_checkpoint"] = True


    # ── Sub-agent construction helpers (used by _execute_agent and
    # register_spawn_capability) ───────────────────────────────────────

    def _create_sub_registry(
        self,
        capability_profile: str = "full",
        write_scope: list[str] | None = None,
    ) -> "ToolRegistry":
        """The registry a sub-agent sees: one declared view of the parent's.

        This used to build a fresh registry and filter by hand, so the session
        scope and the sub-agent scope implemented the same copy twice and a
        rule could live in only one of them.  It is now the parent's own
        ``derive`` call, and the profile is what chooses the view.
        """
        normalized_profile = str(capability_profile or "").strip().lower()
        if normalized_profile not in CAPABILITY_PROFILES:
            raise ValueError(
                f"unsupported capability_profile: {capability_profile!r}"
            )
        allowed_capabilities: set[str] | None = None
        if normalized_profile in {"read_only", "research"}:
            allowed_capabilities = {"read", "output_write"}
        elif normalized_profile == "implementation":
            allowed_capabilities = {"read", "output_write"}
            if write_scope:
                allowed_capabilities.add("workspace_write")

        context: dict[str, Any] = {"capability_profile": normalized_profile}
        if write_scope:
            context["write_scope"] = list(write_scope)
        return self.registry.derive(
            ToolView(
                capabilities_include=(
                    None
                    if allowed_capabilities is None
                    else frozenset(allowed_capabilities)
                ),
                # Sub-agents must never spawn further sub-agents -- excluded by
                # capability rather than by name, so a differently-named
                # spawner falls under the same rule.
                capabilities_exclude=frozenset({"orchestration"}),
            ),
            context=context,
        )

    def _compose_sub_system_prompt(
        self,
        sub_registry: "ToolRegistry",
        system_suffix: str = "",
    ) -> tuple[str, Optional[AgentContext]]:
        """See :func:`agent.core.subagent_context.compose_sub_system_prompt`."""
        return compose_sub_system_prompt(
            sub_registry,
            base_system_prompt=self._base_system_prompt,
            workspace_root=self.workspace_root,
            active_context=self.current_context(),
            system_suffix=system_suffix,
            compose=_compose_system_prompt,
            skill_catalog_type=SkillCatalog,
        )

    @staticmethod
    def _propagate_sub_metadata(
        sub_ctx: AgentContext,
        active_ctx: Optional[AgentContext],
    ) -> None:
        """See :func:`agent.core.subagent_context.propagate_sub_metadata`."""
        propagate_sub_metadata(sub_ctx, active_ctx)

    def _create_sub_agent(self, sub_registry: "ToolRegistry") -> "BaseAgent":
        """Build one sub-agent from this agent's configuration.

        See :mod:`agent.core.subagent_factory`.  The transport is inherited
        deliberately: it carries the model -> provider routing table, and
        rebuilding it from ``client`` would pin the sub-agent to the active
        provider, rejecting a per-turn model override from another provider.
        """
        return build_sub_agent(
            SubAgentSpec(
                client=self.client,
                transport=self._transport,
                registry=sub_registry,
                settings=settings_from(self),
                context_manager=self._context_manager_for(),
                # The parent's buses: a sub-agent's turns are this composition's turns.
                event_bus=self._event_bus,
                llm_event_bus=self._llm_event_bus,
                tool_event_bus=self._tool_event_bus,
            ),
            agent_type=BaseAgent,
        )

    async def _call_llm(
        self,
        prompt: str,
        *,
        system: str,
        max_tokens: int = 1024,
    ) -> str | None:
        """Make a lightweight, tool-free LLM call.

        Returns the response text, or None if the call fails.  Used by
        _summarize_text and _summarize_rendezvous_round.  Dispatch lives
        in the transport — provider-specific dispatch is not the agent's job.
        """
        return await self._transport.simple_chat(
            model=self._effective_model(),
            max_tokens=max_tokens,
            system=system,
            prompt=prompt,
        )

    async def _summarize_text(
        self, text: str, role: str, task: str
    ) -> str:
        """Summarize a sub-agent result via a lightweight LLM call.

        Falls back to a smart boundary-aware truncation when the client is
        unavailable or the API call fails.
        """
        prompt = (
            f"Summarize the output from a sub-agent ({role}). "
            f"Its task was: {task[:500]}\n\n"
            f"Output to summarize:\n{text[:12000]}\n\n"
            f"Include all key findings, decisions, concrete data, and "
            f"files created or modified. Keep the summary concise."
        )
        summary = await self._call_llm(
            prompt,
            system="You are a precise summarizer. Extract and preserve all substantive facts, decisions, and outputs.",
            max_tokens=1024,
        )
        if summary:
            return summary
        # Fallback: keep first result_content_max_chars with a boundary break
        limit = self._result_content_limit()
        if len(text) <= limit:
            return text
        boundary = text.rfind("\n", 0, limit)
        if boundary > limit // 2:
            return text[:boundary].rstrip() + "\n\n...(full result in memory)"
        return text[:limit].rstrip() + "\n\n...(full result in memory)"

    async def _execute_agent(
        self,
        role: str,
        task: str,
        *,
        system_suffix: str = "",
        expected_output: str = "",
        output_contract: dict[str, Any] | None = None,
        write_scope: list[str] | None = None,
        capability_profile: str = "full",
        handoff: dict[str, Any] | None = None,
        run_id: str = "",
    ) -> dict[str, Any]:
        """Execute a sub-agent and return a structured result dict.

        This is the single execution primitive for all sub-agent runs —
        the spawn_agent tool and the orchestrated _execute_subtask_spec
        both route through it, eliminating the JSON round-trip that
        existed when _execute_subtask_spec called spawn_agent via
        registry.call().
        """
        prepared = prepare_sub_agent(
            role=role,
            task=task,
            system_suffix=system_suffix,
            expected_output=expected_output,
            output_contract=output_contract,
            write_scope=write_scope,
            capability_profile=capability_profile,
            plugin_catalog=self.plugin_catalog,
            normalize_output_contract=self._normalize_output_contract,
            with_output_contract=self._with_output_contract,
            create_sub_registry=self._create_sub_registry,
            create_sub_agent=self._create_sub_agent,
            compose_system_prompt=self._compose_sub_system_prompt,
        )
        normalized_scope = list(prepared.normalized_scope)
        normalized_output_contract = prepared.output_contract
        shaped_task = prepared.shaped_task
        sub_registry = prepared.sub_registry
        sub_agent = prepared.sub_agent
        sys_prompt = prepared.system_prompt
        active_ctx = prepared.active_context

        def _fresh_sub_ctx() -> AgentContext:
            ctx = AgentContext(role=role, system_prompt=sys_prompt)
            ctx.metadata["_orchestration_child"] = True
            self._propagate_sub_metadata(ctx, active_ctx)
            # The handoff is per-spawn, so it belongs in the turn's message and
            # not in the system prompt: two sub-agents of the same role share a
            # rendered prompt only while nothing per-spawn sits in it, and a
            # shared head is what lets the provider serve their common prefix
            # from its cache.  `_prepare_turn` picks this up the same way it
            # picks up the session checkpoint.
            if handoff:
                ctx.metadata["_handoff_block"] = (
                    "## Handoff data from upstream\n"
                    + self._bounded_json(handoff, limit=6000)
                )
            return ctx

        sub_ctx = _fresh_sub_ctx()
        self._emit_subagent_event(
            SubAgentProgressEvent(
                kind="agent_started",
                role=role,
                task=shaped_task,
                message=f"{role} started: {shaped_task[:120]}",
                metrics={
                    "capability_profile": capability_profile,
                    "write_scope_count": len(normalized_scope),
                },
            )
        )
        # The context in flight, kept in a cell the loop's callbacks share.
        # A retry must run on a *fresh* context (see _fresh_sub_ctx), so the
        # attempt being sent and the attempt being read for partial text must
        # be the same one -- capturing the initial `sub_ctx` in both closures
        # made a retry send the new context and salvage text from the old.
        _current: dict[str, AgentContext] = {"ctx": sub_ctx}

        def _new_context() -> AgentContext:
            ctx = _fresh_sub_ctx()
            _current["ctx"] = ctx
            return ctx

        outcome = await run_with_retries(
            role=role,
            shaped_task=shaped_task,
            capability_profile=capability_profile,
            write_scope_count=len(normalized_scope),
            configured_retries=self.sub_agent_retries,
            timeout_seconds=self.sub_agent_timeout_seconds,
            send=lambda: sub_agent.send_message(_current["ctx"], shaped_task),
            fresh_context=_new_context,
            current_context=lambda: _current["ctx"],
            emit=lambda **fields: self._emit_subagent_event(
                SubAgentProgressEvent(
                    kind=fields.pop("kind"),
                    role=fields.pop("role"),
                    task=fields.pop("task"),
                    message=fields.pop("message", ""),
                    metrics=fields.pop("metrics", None) or {},
                )
            ),
            format_error=self._format_agent_error,
        )
        result = outcome.result
        failure_payload = outcome.payload
        started_at = outcome.started_at
        if result is None:
            # No attempt succeeded: either the budget ran out before a retry
            # could start, or the last attempt failed.  This is a conditional
            # return, not an unconditional one -- an earlier version of this
            # splice lost that distinction and reported "no result" for every
            # successful sub-agent.
            return failure_payload or {
                "ok": False,
                "role": role,
                "task": shaped_task,
                "error": "sub-agent produced no result",
            }
        payload = build_result_payload(
            result=result,
            role=role,
            shaped_task=shaped_task,
            expected_output=expected_output,
            output_contract=normalized_output_contract,
            validate_output_contract=self._validate_output_contract,
        )
        elapsed = time.monotonic() - started_at
        self._emit_subagent_event(
            SubAgentProgressEvent(
                kind="agent_finished" if result.error is None else "agent_failed",
                role=role,
                task=shaped_task,
                message=(
                    f"{role} finished in {elapsed:.1f}s"
                    if result.error is None
                    else f"{role} failed in {elapsed:.1f}s: {result.error}"
                ),
                metrics={
                    "capability_profile": capability_profile,
                    "write_scope_count": len(normalized_scope),
                    "duration_seconds": elapsed,
                    "tool_call_count": len(result.tool_calls_made),
                },
            )
        )
        # The full text goes to memory, the summary to the parent; see
        # agent/core/subagent_finalize.py for why they differ.
        finalized = await finalize_sub_agent(
            payload,
            full_content=payload["content"],
            role=role,
            shaped_task=shaped_task,
            run_id=run_id,
            content_limit=self._result_content_limit(),
            summarize=self._summarize_text,
            context_manager=self._context_manager_for(),
            new_id=shared._new_id,
            now=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            entry_type=LTMEntry,
            suppress=shared._suppress_with_log,
        )
        payload = finalized.payload
        return payload


    def register_spawn_capability(
        self, base_system_prompt: str, workspace_root: Optional[Path] = None
    ) -> None:
        """Register lightweight delegation tools.

        The main agent can call spawn_agent one or more times in a single turn.
        Multiple calls are executed in parallel (via asyncio.gather in send_message).
        Sub-agents receive all regular tools but NOT spawn_agent, preventing recursion.
        """
        parent = self  # captured reference to the parent agent
        self.workspace_root = workspace_root.resolve() if workspace_root else None
        self._base_system_prompt = base_system_prompt

        async def spawn_agent(
            role: str,
            task: str,
            system_suffix: str = "",
            expected_output: str = "",
            output_contract: dict[str, Any] | None = None,
            write_scope: list[str] | None = None,
            capability_profile: str = "full",
            **_kwargs: Any,
        ) -> dict:
            # depends_on and coordination_mode are read from the tool_use
            # input dict by the orchestration dispatch layer; the function
            # accepts them via **kwargs to avoid TypeError from ToolRegistry.
            # Execution is delegated to _execute_agent — the single primitive
            # shared by the spawn_agent tool and the orchestration runtime.
            return await parent._execute_agent(
                role=role,
                task=task,
                system_suffix=system_suffix,
                expected_output=expected_output,
                output_contract=output_contract,
                write_scope=write_scope,
                capability_profile=capability_profile,
            )

        self.registry.register(
            "spawn_agent",
            (
                "Spawn a specialized sub-agent to handle a task from a particular perspective. "
                "When called multiple times in one response, the runtime derives direct, parallel, "
                "pipeline, or rendezvous execution from the explicit depends_on and coordination_mode fields. "
                "Each sub-agent has a fresh context and only the tools allowed by its capability profile."
            ),
            {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": (
                            "Optional unique logical subtask id. Required when another "
                            "subtask refers to this one through depends_on."
                        ),
                    },
                    "role": {
                        "type": "string",
                        "description": (
                            "Role / persona of the sub-agent "
                            "(e.g. 'researcher', 'critic', 'implementer', 'devil's advocate')"
                        ),
                    },
                    "task": {
                        "type": "string",
                        "description": "The specific task or question for this sub-agent.",
                    },
                    "system_suffix": {
                        "type": "string",
                        "description": (
                            "Optional extra instructions appended to the system prompt "
                            "to shape this sub-agent's behavior."
                        ),
                    },
                    "expected_output": {
                        "type": "string",
                        "description": "Optional structured description of the desired deliverable.",
                    },
                    "output_contract": {
                        "type": "object",
                        "description": (
                            "Optional runtime-validated postconditions for the deliverable. "
                            "Supports format='json', required_keys, and required_files."
                        ),
                        "properties": {
                            "format": {
                                "type": "string",
                                "description": "Optional deliverable format. Supported value: 'json'.",
                            },
                            "required_keys": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Required top-level keys when the deliverable is JSON.",
                            },
                            "required_files": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Files that must exist when the sub-agent finishes.",
                            },
                        },
                    },
                    "write_scope": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Files or directories this sub-agent may modify. Workspace writes are "
                            "only enabled through an explicit write_scope; required when "
                            "capability_profile is 'implementation'."
                        ),
                    },
                    "capability_profile": {
                        "type": "string",
                        "enum": sorted(CAPABILITY_PROFILES),
                        "description": (
                            "Optional capability profile. Use 'read_only' for analysis workers "
                            "and 'implementation' for code-changing workers. 'implementation' "
                            "requires an explicit write_scope so the worker can modify workspace files."
                        ),
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional upstream subtask ids for internal orchestration.",
                    },
                    "coordination_mode": {
                        "type": "string",
                        "enum": ["parallel", "pipeline", "rendezvous"],
                        "description": (
                            "Optional explicit coordination mode for grouped spawn calls. "
                            "Use pipeline with depends_on for a DAG, or rendezvous for "
                            "bounded multi-round coordination. All calls in a batch that "
                            "set this field must use the same value."
                        ),
                    },
                    "early_exit": {
                        "type": "boolean",
                        "description": (
                            "When true, if this agent succeeds the whole batch stops: still-running "
                            "agents are cancelled and later pipeline stages are skipped. Use for "
                            "tasks where any single finding completes the goal. Supported for "
                            "parallel and pipeline subtasks, not rendezvous."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "minimum": 0,
                        "description": "Optional per-subtask timeout. Zero uses the agent default.",
                    },
                    "continue_on_failure": {
                        "type": "boolean",
                        "description": "For pipeline nodes, allow dependent nodes to receive a failure summary.",
                    },
                },
                "required": ["role", "task"],
            },
            spawn_agent,
            source="runtime:spawn",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. SELF-EVOLUTION
# ─────────────────────────────────────────────────────────────────────────────

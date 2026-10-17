from __future__ import annotations
from agent.core import subtask_execution
from agent.core import agent_facades
from agent.core import loop_messages
from agent.core import provider_errors
# aliased: `from agent.core.bounded_text import bounded_text` below binds the *function* of
# that name, shadowing the module -- and the wrappers call module-level functions.
from agent.core import bounded_text as bounded_text_module
from agent.core import progress_events

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

from agent.core.tool_calls import _execute_regular_tool_calls
from agent.core.turn_state import (
    DEFAULT_SYSTEM_PROMPT,
    AgentContext,
    AgentResult,
    SubAgentProgressEvent,
    _ContentFilterRecovery,
    _TaskLocalContextStack,
    _TurnLoopState,
)
logger = logging.getLogger(__name__)

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





from agent.core.turn_context import interaction_log as _interaction_log  # noqa: E402
from agent.core.turn_context import preview_text as _preview_text  # noqa: E402
from agent.core.turn_context import trace_latency as _trace_latency  # noqa: E402




















_ = _TaskLocalContextStack  # publicly importable for any third-party caller






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
        progress_events._emit_subagent_event(
            event=event,
        )


    @staticmethod
    def _format_role_list(roles: list[str], *, limit: int = 3) -> str:
        return progress_events._format_role_list(
            roles=roles,
            limit=limit,
        )


    def _runtime_progress_message(self, kind: str, payload: dict[str, Any]) -> str:
        return progress_events._runtime_progress_message(
            kind=kind,
            payload=payload,
        )


    def _emit_runtime_progress_event(self, kind: str, payload: dict[str, Any]) -> None:
        progress_events._emit_runtime_progress_event(
            kind=kind,
            payload=payload,
        )


    def current_context(self) -> Optional["AgentContext"]:
        return _active_agent_context.get()

    def _context_manager_for(
        self,
        ctx: Optional["AgentContext"] = None,
    ) -> Optional[ContextManager]:
        return agent_facades._context_manager_for(
            ctx=ctx, context_manager=self.context_manager, current_context=self.current_context
        )


    def _effective_model(self, ctx: Optional["AgentContext"] = None) -> str:
        return agent_facades._effective_model(
            ctx=ctx, model=self.model, current_context=self.current_context
        )


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
        return agent_facades._result_content_limit(result_content_max_chars=self.result_content_max_chars)


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
        return bounded_text_module._with_upstream_summaries(
            spec=spec,
            upstream_summaries=upstream_summaries,
            upstream_results=upstream_results,
        )


    def _with_lead_summary(
        self,
        spec: SubtaskSpec,
        lead_summary: str,
        lead_structured_context: dict[str, Any] | None = None,
    ) -> SubtaskSpec:
        return bounded_text_module._with_lead_summary(
            spec=spec,
            lead_summary=lead_summary,
            lead_structured_context=lead_structured_context,
        )


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
        return agent_facades._output_contract_path_resolver(registry=self.registry, workspace_root=self.workspace_root)


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
        return agent_facades._default_capability_profile(
            role=role,
            task=task,
            explicit=explicit,
            orchestration_mode=orchestration_mode, spawn_planner=self._spawn_planner
        )


    def _spawn_tool_use_to_spec(
        self,
        tool_use: dict[str, Any],
        *,
        index: int,
        orchestration_decision: OrchestrationDecision,
    ) -> SubtaskSpec:
        """See :meth:`agent.core.spawn_planning.SpawnPlanner.spawn_tool_use_to_spec`."""
        return agent_facades._spawn_tool_use_to_spec(
            tool_use=tool_use,
            index=index,
            orchestration_decision=orchestration_decision, spawn_planner=self._spawn_planner
        )


    def _max_agents_per_turn(self) -> int:
        """Total sub-agents one turn may run, independent of concurrency."""
        return agent_facades._max_agents_per_turn(spawn_planner=self._spawn_planner)


    def _spawn_result_payload(
        self,
        spec: SubtaskSpec,
        result: SubtaskResult,
        *,
        extra: dict[str, Any] | None = None,
    ) -> str:
        """See :meth:`agent.core.spawn_planning.SpawnPlanner.spawn_result_payload`."""
        return agent_facades._spawn_result_payload(
            spec=spec,
            result=result,
            extra=extra, spawn_planner=self._spawn_planner
        )


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
        return agent_facades._derive_execution_mode_from_spawn_calls(
            spawn_calls=spawn_calls, spawn_planner=self._spawn_planner
        )


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
        return agent_facades._canonicalize_write_scope(
            raw_scope=raw_scope, registry=self.registry, workspace_root=self.workspace_root
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
        return await subtask_execution.run_rendezvous_subtasks(
            specs=specs,
            max_rounds=max_rounds,
            telemetry=telemetry,
            max_parallel_agents=self.max_parallel_agents,
            execute_subtask_spec=self._execute_subtask_spec,
            summarize_rendezvous_round=self._summarize_rendezvous_round,
            canonicalize_write_scope=self._canonicalize_write_scope,
            emit_runtime_progress_event=self._emit_runtime_progress_event,
            with_lead_summary=self._with_lead_summary,
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

        Callers take what they need from the object this returns.  Six
        pass-throughs used to sit here -- ``def _output_room(self, ...): return
        self._token_budget.output_room(...)`` and five like it -- and existed
        only so the turn loop could read the arithmetic *off the agent*.
        ``LoopDeps.of`` reads the budget now, which is the same arithmetic
        without the agent in the middle.
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
        """This agent's retry policy, in the shape the retry leg takes."""
        return agent_facades._retry_policy(llm_event_bus=self._llm_event_bus, llm_max_retries=self.llm_max_retries, llm_retry_base_delay=self.llm_retry_base_delay, is_llm_retryable=self._is_llm_retryable)











    @staticmethod
    def _clear_context_restart_message(
        tool_calls: list[dict],
        results: list[str],
    ) -> str | None:
        """Return a deferred clear-context restart message from a tool batch."""
        return loop_messages._clear_context_restart_message(
            tool_calls=tool_calls,
            results=results,
        )


    def _format_agent_error(self, exc: Exception) -> str:
        return provider_errors._format_agent_error(
            exc=exc,
        )



    @staticmethod
    @staticmethod
    def _is_account_exhausted(exc: Exception) -> bool:
        """See :func:`agent.core.llm_errors.is_account_exhausted`."""
        return provider_errors._is_account_exhausted(
            exc=exc,
        )


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
        return loop_messages._build_tool_loop_stuck_response(
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

        The implementation is :func:`agent.core.post_turn.post_turn_maintenance`; this is the
        name the turn loop, the CLI and the tests have always called.
        """
        from agent.core.post_turn import post_turn_maintenance

        post_turn_maintenance(
            ctx_mgr=ctx_mgr,
            agent=agent,
            ctx=ctx,
            user_content=user_content,
            assistant_content=assistant_content,
            channel=channel,
            record_kwargs=record_kwargs,
            memory_worker=memory_worker,
            system_prompt=system_prompt,
            task_context=task_context,
            error=error,
        )




    # ── Sub-agent construction helpers (used by _execute_agent and
    # register_spawn_capability) ───────────────────────────────────────

    def _create_sub_registry(
        self,
        capability_profile: str = "full",
        write_scope: list[str] | None = None,
    ) -> 'ToolRegistry':
        """The registry a sub-agent sees: one declared view of the parent's."""
        return agent_facades._create_sub_registry(
            capability_profile=capability_profile,
            write_scope=write_scope, registry=self.registry
        )


    def _compose_sub_system_prompt(
        self,
        sub_registry: "ToolRegistry",
        system_suffix: str = "",
    ) -> tuple[str, Optional[AgentContext]]:
        """See :func:`agent.core.subagent_context.compose_sub_system_prompt`."""
        return agent_facades._compose_sub_system_prompt(
            sub_registry=sub_registry,
            system_suffix=system_suffix, workspace_root=self.workspace_root, base_system_prompt=self._base_system_prompt, current_context=self.current_context
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
        """Make a lightweight, tool-free LLM call."""
        return await agent_facades._call_llm(
            prompt=prompt,
            system=system,
            max_tokens=max_tokens, transport=self._transport, model=self.model, current_context=self.current_context
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

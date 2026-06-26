from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import inspect
import json
import logging
from pathlib import Path
import re
import time
from typing import Any, Awaitable, Callable, Optional

import agent as agent_module
from agent import shared
from agent.config import _compose_system_prompt
from agent.core.attachments import MessageAttachment, format_attachment_context
from agent.core.output import CliOutputSink, _active_sink
from agent.memory.system import ContextManager, LTMEntry
from agent.orchestration.runtime import (
    RendezvousDirective,
    SubtaskResult,
    SubtaskSpec,
    run_parallel_subtasks as _run_parallel_subtasks,
    run_pipeline_subtasks as _run_pipeline_subtasks,
    run_rendezvous_round as _run_rendezvous_round,
)
from agent.orchestration.planner import OrchestrationDecision, OrchestrationPlanner
from agent.pathing import canonicalize_user_path, resolve_workspace_path
from agent.plugins.catalog import PluginCatalog
from agent.runtime.heartbeat import HeartbeatWriter
from agent.tools.executor import RegularToolExecutor
from agent.skills.catalog import SkillCatalog
from agent.tools.runtime import ToolRegistry
from agent.security.content_filter import (
    ContentFilter,
    default_model_path,
    filter_tool_results,
    summarize_tool_result,
)

DEFAULT_SYSTEM_PROMPT = agent_module.DEFAULT_SYSTEM_PROMPT
logger = logging.getLogger(__name__)

# Context variable so built-in tools can access the active AgentContext
_active_agent_context: contextvars.ContextVar[Optional["AgentContext"]] = (
    contextvars.ContextVar("active_agent_context", default=None)
)


def _trace_latency(stage: str, **fields: object) -> None:
    shared._trace_latency("agent", stage, **fields)


def _preview_text(text: object, limit: int = 80) -> str:
    return shared._preview_text(text, limit=limit)


def _interaction_log(event: str, **fields: object) -> None:
    shared._interaction_log("agent", event, **fields)

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


class BaseAgent:
    """Core agent: streams Claude, handles tool_use loop."""

    _MAX_TRUNCATION_CONTINUATIONS = 2
    _TOOL_LOOP_REPEAT_THRESHOLD = 3
    _TOOL_LOOP_UNPRODUCTIVE_THRESHOLD = 4
    _CONTINUE_PROMPT = (
        "Continue exactly from where you left off. "
        "Do not repeat previous text. "
        "Do not restart the answer."
    )

    def __init__(
        self,
        client: Any,
        registry: ToolRegistry,
        model: str = shared.DEFAULT_MODEL,
        max_tokens: int = shared.DEFAULT_MAX_TOKENS,
        api_format: str = "anthropic",
        supports_vision: bool = False,
    ):
        self.client = client
        self.registry = registry
        self.api_format = api_format
        self.supports_vision = supports_vision
        self.model = model
        self.max_tokens = max_tokens
        from agent.core.transport import build_transport
        self._transport = build_transport(api_format, client)
        self.context_manager: Optional[ContextManager] = None
        self.plugin_catalog: Optional["PluginCatalog"] = None
        self.max_parallel_agents = shared.DEFAULT_MAX_PARALLEL_AGENTS
        self.sub_agent_timeout_seconds = shared.DEFAULT_SUB_AGENT_TIMEOUT_SECONDS
        self.sub_agent_retries = shared.DEFAULT_SUB_AGENT_RETRIES
        self.max_tool_call_iterations = shared.MAX_TOOL_CALL_ITERATIONS
        self.max_rendezvous_rounds = 2  # Mirrors OrchestrationDecision default.
        self.result_content_max_chars = shared.DEFAULT_RESULT_CONTENT_MAX_CHARS
        self.llm_max_retries = shared.DEFAULT_LLM_MAX_RETRIES
        self.llm_retry_base_delay = shared.DEFAULT_LLM_RETRY_BASE_DELAY
        self.workspace_root: Optional[Path] = None
        self._base_system_prompt: str = ""
        self.content_filter: ContentFilter = ContentFilter.load(default_model_path())
        self._content_filter_threshold: float = 0.7

    def _image_content_block(self, attachment: MessageAttachment) -> dict[str, Any]:
        data = base64.b64encode(attachment.local_path.read_bytes()).decode("ascii")
        mime_type = attachment.mime_type or "application/octet-stream"
        return self._transport.image_content_block(mime_type, data)

    def _build_user_message_content(
        self,
        user_message: str,
        attachments: tuple[MessageAttachment, ...] = (),
    ) -> str | list[dict[str, Any]]:
        if not attachments:
            return user_message

        direct_images: list[MessageAttachment] = []
        fallback_attachments: list[MessageAttachment] = []
        if self.supports_vision:
            for attachment in attachments:
                if attachment.kind == "image" and attachment.local_path.is_file():
                    direct_images.append(attachment)
                else:
                    fallback_attachments.append(attachment)
        else:
            fallback_attachments = list(attachments)

        fallback_context = format_attachment_context(fallback_attachments)
        text = user_message
        if fallback_context:
            text = f"{user_message}\n\n{fallback_context}" if user_message else fallback_context
        if not direct_images:
            return text

        blocks: list[dict[str, Any]] = []
        if text.strip():
            blocks.append({"type": "text", "text": text})
        blocks.extend(self._image_content_block(attachment) for attachment in direct_images)
        return blocks

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

    def set_model(self, model: str) -> None:
        """Switch the model used for subsequent calls."""
        self.model = model

    def current_context(self) -> Optional["AgentContext"]:
        return _active_agent_context.get()

    def _plan_orchestration(
        self,
        ctx: AgentContext,
        user_message: str,
    ) -> OrchestrationDecision:
        skill_catalog: Optional[SkillCatalog] = ctx.metadata.get("skill_catalog")
        planner = OrchestrationPlanner.from_skill_catalog(skill_catalog)
        return planner.decide(
            user_message,
            tools_enabled=ctx.tools_enabled,
            has_spawn_agent=bool(self.registry.tools_with_capability("orchestration")),
        )

    async def _execute_subtask_spec(self, spec: SubtaskSpec) -> SubtaskResult:
        # Call the single execution primitive directly — no JSON round-trip
        # through the tool registry.
        payload = await self._execute_agent(
            role=spec.role,
            task=spec.task,
            expected_output=spec.expected_output,
            output_contract=dict(spec.output_contract) if spec.output_contract else None,
            write_scope=list(spec.write_scope) if spec.write_scope else None,
            capability_profile=spec.capability_profile,
            handoff=dict(spec.handoff) if spec.handoff else None,
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
        )

    @staticmethod
    def _summarize_subtask_result(content: str, limit: int = 400) -> str:
        text = str(content or "").strip()
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

    @staticmethod
    def _append_named_block(
        base_text: str,
        heading: str,
        lines: list[str],
    ) -> str:
        if not lines:
            return base_text
        extra = f"{heading}\n" + "\n".join(lines)
        if not base_text:
            return extra
        return f"{base_text}\n\n{extra}"

    @staticmethod
    def _stable_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _with_upstream_summaries(
        self,
        spec: SubtaskSpec,
        upstream_summaries: dict[str, str],
        upstream_results: dict[str, SubtaskResult] | None = None,
    ) -> SubtaskSpec:
        lines = [f"- {dep}: {summary}" for dep, summary in upstream_summaries.items()]
        task = self._append_named_block(spec.task, "Upstream summaries:", lines)
        handoff: dict[str, Any] = dict(spec.handoff)
        structured_lines = []
        for dep, result in (upstream_results or {}).items():
            if result.structured_content is not None:
                handoff[dep] = result.structured_content
                structured_lines.append(
                    f"- {dep}: {self._stable_json(result.structured_content)}"
                )
            if result.full_content:
                handoff.setdefault(f"{dep}_full_content", result.full_content)
        task = self._append_named_block(
            task,
            "Upstream structured results:",
            structured_lines,
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
            [lead_summary] if lead_summary else [],
        )
        structured_lines = []
        for dep, value in (lead_structured_context or {}).items():
            structured_lines.append(f"- {dep}: {self._stable_json(value)}")
        task = self._append_named_block(
            task,
            "Lead structured results:",
            structured_lines,
        )
        handoff: dict[str, Any] = dict(spec.handoff)
        for dep, value in (lead_structured_context or {}).items():
            handoff[dep] = value
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
            detail = result.summary or result.content[:600] or result.error or ""
            lines.append(
                f"Agent [{result.id}] (status={status}): {detail}"
            )
        results_text = "\n".join(
            f"- {line.rstrip()}" for line in lines
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
            f"- stop: true if consensus is clear or further rounds would not add value\n"
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
                # Strip markdown code fences if present
                if resp_text.startswith("```"):
                    resp_text = resp_text.split("\n", 1)[-1]
                    if resp_text.endswith("```"):
                        resp_text = resp_text[:-3].strip()
                directive_data = json.loads(resp_text)
                return RendezvousDirective(
                    summary=str(directive_data.get("summary", "")),
                    stop=bool(directive_data.get("stop", False)),
                    continue_with=(
                        list(directive_data["continue_with"])
                        if isinstance(directive_data.get("continue_with"), list)
                        else None
                    ),
                )
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
    def _with_expected_output_contract(task: str, expected_output: str) -> str:
        if not expected_output:
            return task
        return BaseAgent._append_named_block(
            task,
            "Expected output contract:",
            [
                expected_output,
                "Return the final deliverable inside this exact block:",
                "<deliverable>",
                "<your deliverable here>",
                "</deliverable>",
            ],
        )

    @staticmethod
    def _mapping_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        return {}

    @staticmethod
    def _normalize_output_contract(output_contract: dict[str, Any] | None) -> dict[str, Any]:
        contract = BaseAgent._mapping_dict(output_contract)
        format_name = str(contract.get("format", "") or "").strip().lower()
        required_keys = [
            str(item)
            for item in contract.get("required_keys", [])
            if str(item).strip()
        ]
        required_files = [
            str(item)
            for item in contract.get("required_files", [])
            if str(item).strip()
        ]
        normalized: dict[str, Any] = {}
        if format_name:
            normalized["format"] = format_name
        if required_keys:
            normalized["required_keys"] = required_keys
        if required_files:
            normalized["required_files"] = required_files
        return normalized

    @staticmethod
    def _output_contract_requires_deliverable(
        expected_output: str,
        output_contract: dict[str, Any] | None,
    ) -> bool:
        contract = BaseAgent._normalize_output_contract(output_contract)
        return bool(
            str(expected_output or "").strip()
            or contract.get("format") == "json"
            or contract.get("required_keys")
        )

    @classmethod
    def _with_output_contract(
        cls,
        task: str,
        expected_output: str,
        output_contract: dict[str, Any] | None,
    ) -> str:
        contract = cls._normalize_output_contract(output_contract)
        if not expected_output and not contract:
            return task
        requires_deliverable = cls._output_contract_requires_deliverable(
            expected_output,
            contract,
        )
        lines: list[str] = []
        if expected_output:
            lines.append(expected_output)
        if contract.get("format") == "json":
            lines.append("The deliverable inside <deliverable> must be a JSON object.")
        required_keys = contract.get("required_keys", [])
        if required_keys:
            lines.append(
                "The JSON deliverable must include these keys: "
                + ", ".join(required_keys)
            )
        required_files = contract.get("required_files", [])
        if required_files:
            lines.append(
                "These files must exist when you finish: "
                + ", ".join(required_files)
            )
        if requires_deliverable:
            lines.extend(
                [
                    "Return the final deliverable inside this exact block:",
                    "<deliverable>",
                    "<your deliverable here>",
                    "</deliverable>",
                ]
            )
        return cls._append_named_block(task, "Expected output contract:", lines)

    @staticmethod
    def _extract_deliverable_block(content: str) -> str | None:
        if not content:
            return None
        match = re.search(
            r"<deliverable>\s*(.*?)\s*</deliverable>",
            str(content),
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return None
        text = match.group(1).strip()
        return text or None

    def _resolve_output_contract_path(self, raw_path: str) -> Path:
        output_dir_str = self.registry.get_context("output_dir")
        output_dir = Path(output_dir_str) if output_dir_str else None
        if output_dir is not None:
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                return (output_dir / candidate).resolve(strict=False)
        workspace_root = self.workspace_root or Path.cwd()
        resolved, _root_kind = resolve_workspace_path(
            raw_path,
            workspace_root=workspace_root,
            output_dir=output_dir,
        )
        return resolved

    def _validate_output_contract(
        self,
        content: str,
        *,
        expected_output: str,
        output_contract: dict[str, Any] | None,
    ) -> tuple[bool, str, dict[str, Any] | None, str | None]:
        contract = self._normalize_output_contract(output_contract)
        if not expected_output and not contract:
            return True, content, None, None

        requires_deliverable = self._output_contract_requires_deliverable(
            expected_output,
            contract,
        )
        deliverable = (
            self._extract_deliverable_block(content or "")
            if requires_deliverable
            else None
        )
        if requires_deliverable and deliverable is None:
            return (
                False,
                content,
                None,
                "Expected output contract not satisfied: missing <deliverable> block",
            )

        structured_content: dict[str, Any] | None = None
        normalized_content = deliverable if deliverable is not None else content
        requires_json = contract.get("format") == "json" or bool(
            contract.get("required_keys")
        )
        if requires_json:
            # requires_json ⇒ requires_deliverable (see _output_contract_requires_deliverable),
            # so we've already returned earlier if deliverable is None.
            assert deliverable is not None
            try:
                parsed = json.loads(deliverable)
            except Exception:
                return (
                    False,
                    deliverable,
                    None,
                    "Expected output contract not satisfied: deliverable is not valid JSON",
                )
            if not isinstance(parsed, dict):
                return (
                    False,
                    deliverable,
                    None,
                    "Expected output contract not satisfied: deliverable JSON must be an object",
                )
            required_keys = contract.get("required_keys", [])
            missing_keys = [key for key in required_keys if key not in parsed]
            if missing_keys:
                return (
                    False,
                    deliverable,
                    parsed,
                    "Expected output contract not satisfied: missing required deliverable keys: "
                    + ", ".join(missing_keys),
                )
            structured_content = parsed
            normalized_content = json.dumps(parsed, ensure_ascii=False, sort_keys=True)

        required_files = contract.get("required_files", [])
        if required_files:
            missing_files: list[str] = []
            for raw_path in required_files:
                try:
                    resolved_path = self._resolve_output_contract_path(raw_path)
                except ValueError as exc:
                    return (
                        False,
                        normalized_content,
                        structured_content,
                        "Expected output contract not satisfied: invalid required output file path: "
                        + str(exc),
                    )
                if not resolved_path.exists():
                    missing_files.append(raw_path)
            if missing_files:
                return (
                    False,
                    normalized_content,
                    structured_content,
                    "Expected output contract not satisfied: missing required output file(s): "
                    + ", ".join(missing_files),
                )

        return True, normalized_content, structured_content, None

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not value:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, (list, tuple, set)):
            return [str(item) for item in value if str(item).strip()]
        text = str(value).strip()
        return [text] if text else []

    @staticmethod
    def _looks_like_implementation_work(role: str, task: str) -> bool:
        text = f"{role} {task}".lower()
        tokens = (
            "implement",
            "implementation",
            "engineer",
            "developer",
            "coder",
            "patch",
            "fix",
            "edit",
            "modify",
            "refactor",
            "code",
            "file",
            "实现",
            "修复",
            "修改",
            "重构",
            "编码",
        )
        return any(token in text for token in tokens)

    def _default_capability_profile(
        self,
        role: str,
        task: str,
        explicit: str,
        *,
        orchestration_mode: str,
    ) -> str:
        if explicit:
            return explicit
        if orchestration_mode == "direct":
            return "full"
        if self._looks_like_implementation_work(role, task):
            return "implementation"
        return "read_only"

    def _spawn_tool_use_to_spec(
        self,
        tool_use: dict[str, Any],
        *,
        index: int,
        orchestration_decision: OrchestrationDecision,
        previous_spec: SubtaskSpec | None = None,
    ) -> SubtaskSpec:
        tool_input = tool_use.get("input", {})
        role = str(tool_input.get("role", "assistant") or "assistant")
        task = str(tool_input.get("task", "") or "")
        spec_id = str(
            tool_input.get("id")
            or tool_use.get("id")
            or f"spawn-{index}"
        )
        depends_on = self._string_list(tool_input.get("depends_on"))
        if (
            orchestration_decision.mode == "pipeline"
            and not depends_on
            and previous_spec is not None
        ):
            depends_on = [previous_spec.id]
        explicit_profile = str(tool_input.get("capability_profile", "") or "").strip()
        return SubtaskSpec(
            id=spec_id,
            role=role,
            task=task,
            depends_on=depends_on,
            expected_output=str(tool_input.get("expected_output", "") or ""),
            output_contract=self._normalize_output_contract(
                self._mapping_dict(tool_input.get("output_contract"))
            ),
            write_scope=self._string_list(tool_input.get("write_scope")),
            capability_profile=self._default_capability_profile(
                role,
                task,
                explicit_profile,
                orchestration_mode=orchestration_decision.mode,
            ),
            early_exit=bool(tool_input.get("early_exit", False)),
        )

    def _spawn_result_payload(self, spec: SubtaskSpec, result: SubtaskResult) -> str:
        payload: dict[str, Any] = {
            "ok": result.ok,
            "role": spec.role,
            "task": spec.task,
            "content": result.content or "(no output)",
            "tool_calls_made": result.tool_calls_made,
        }
        if result.structured_content is not None:
            payload["structured_content"] = result.structured_content
        if result.error:
            payload["error"] = result.error
        return json.dumps(payload, ensure_ascii=False)

    async def _run_orchestrated_spawn_calls(
        self,
        spawn_calls: list[tuple[int, dict]],
        orchestration_decision: OrchestrationDecision,
    ) -> tuple[list[str], dict[str, Any]]:
        specs: list[SubtaskSpec] = []
        previous_spec: SubtaskSpec | None = None
        for index, (_result_index, tool_use) in enumerate(spawn_calls, start=1):
            spec = self._spawn_tool_use_to_spec(
                tool_use,
                index=index,
                orchestration_decision=orchestration_decision,
                previous_spec=previous_spec,
            )
            specs.append(spec)
            previous_spec = spec

        execution_mode = self._derive_execution_mode_from_spawn_calls(spawn_calls)
        # Honour the planner's explicit mode decision when the runtime
        # would otherwise derive something different.  This ensures the
        # planner is authoritative: keyword-based routing and skill
        # configuration take precedence over the LLM's raw spawn calls.
        planner_mode = str(orchestration_decision.mode or "").strip().lower()
        if planner_mode and planner_mode != "explicit" and planner_mode != execution_mode:
            if planner_mode == "pipeline" and execution_mode == "parallel":
                execution_mode = "pipeline"
            elif planner_mode == "rendezvous" and execution_mode in ("parallel", "pipeline"):
                execution_mode = "rendezvous"
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
                            "error": "skipped because an upstream pipeline stage failed",
                        },
                        ensure_ascii=False,
                    )
                )
                continue
            payloads.append(self._spawn_result_payload(spec, result))
        return payloads, telemetry

    def _derive_execution_mode_from_spawn_calls(
        self,
        spawn_calls: list[tuple[int, dict]],
    ) -> str:
        explicit_modes = {
            str(tu.get("input", {}).get("coordination_mode", "") or "").strip().lower()
            for _, tu in spawn_calls
        }
        explicit_modes.discard("")
        if "rendezvous" in explicit_modes:
            return "rendezvous"
        if any(self._string_list(tu.get("input", {}).get("depends_on")) for _, tu in spawn_calls):
            return "pipeline"
        if len(spawn_calls) > 1:
            return "parallel"
        return "direct"

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
        if self.workspace_root is not None:
            output_dir_str = self.registry.get_context("output_dir")
            output_dir = Path(output_dir_str) if output_dir_str else None
            resolved, _root_kind = resolve_workspace_path(
                raw_scope,
                workspace_root=self.workspace_root,
                output_dir=output_dir,
            )
            return resolved
        return canonicalize_user_path(raw_scope, base_dir=Path.cwd())

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
            return await self._execute_subtask_spec(adjusted)

        return await _run_rendezvous_round(
            specs,
            executor=_executor,
            summarize=self._summarize_rendezvous_round,
            max_rounds=max_rounds,
            canonicalize_write_scope=self._canonicalize_write_scope,
            telemetry=telemetry,
            progress_callback=self._emit_runtime_progress_event,
        )

    # ── Format-aware API helpers ──────────────────────────────────────────

    def _tools_for_api(self, tools: list[dict]) -> Any:
        """Convert tools to the right format; return NOT_GIVEN/None if empty."""
        return self._transport.convert_tools(tools)

    async def _create(self, ctx: "AgentContext", tools: list[dict]) -> Any:
        """Non-streaming API call, returns a normalised response object."""
        return await self._transport.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=ctx.system_prompt,
            messages=ctx.messages,
            tools=tools,
        )

    def _parse_response(self, response: Any) -> tuple[str, str, list[dict]]:
        return self._transport.parse_response(response)

    def _response_completion_error(self, response: Any) -> Optional[str]:
        return self._transport.completion_error(response)

    @staticmethod
    def _merge_continuation_text(prefix: str, continuation: str) -> str:
        """Append continuation text while trimming simple duplicated overlap."""
        if not prefix:
            return continuation
        if not continuation:
            return prefix
        max_overlap = min(len(prefix), len(continuation), 64)
        for size in range(max_overlap, 0, -1):
            if prefix.endswith(continuation[:size]):
                return prefix + continuation[size:]
        return prefix + continuation

    @staticmethod
    def _build_continuation_context(ctx: "AgentContext") -> "AgentContext":
        """Create the minimal context needed for bounded auto-continue requests."""
        return AgentContext(
            agent_id=ctx.agent_id,
            role=ctx.role,
            messages=list(ctx.messages),
            system_prompt=ctx.system_prompt,
            tools_enabled=ctx.tools_enabled,
        )

    async def _continue_truncated_response(
        self,
        ctx: "AgentContext",
        partial_text: str,
    ) -> tuple[str, Optional[str]]:
        """Try to complete a truncated final response with bounded follow-up calls."""
        merged = partial_text
        continuation_error = "Model response was truncated (finish_reason=length)"
        for _ in range(self._MAX_TRUNCATION_CONTINUATIONS):
            continuation_ctx = self._build_continuation_context(ctx)
            continuation_ctx.messages.append(
                {"role": "user", "content": self._CONTINUE_PROMPT}
            )
            response = await self._create(continuation_ctx, [])
            stop_reason, text, tool_uses = self._parse_response(response)
            if stop_reason == "tool_use" and tool_uses:
                break
            merged = self._merge_continuation_text(merged, text)
            continuation_error = self._response_completion_error(response)
            if continuation_error is None:
                return merged, None
        return (
            merged,
            f"Model response remained truncated after "
            f"{self._MAX_TRUNCATION_CONTINUATIONS} auto-continue attempts",
        )

    def _assistant_message(self, response: Any, text: str) -> dict:
        """Build the assistant history entry after a tool_use stop."""
        return self._transport.build_assistant_message(response, text)

    def _tool_result_messages(
        self, tool_calls: list[dict], results: list[str]
    ) -> list[dict]:
        """Build tool-result history entries for both formats."""
        return self._transport.build_tool_result_messages(tool_calls, results)

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
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return "Model request timed out"
        if isinstance(exc, ValueError):
            return f"Invalid model request: {exc}"
        return str(exc) or exc.__class__.__name__

    _retryable_llm_classes_cache: tuple[type[BaseException], ...] | None = None

    @classmethod
    def _retryable_llm_classes(cls) -> tuple[type[BaseException], ...]:
        """Lazy-import SDK error classes once and cache as an isinstance tuple."""
        if cls._retryable_llm_classes_cache is not None:
            return cls._retryable_llm_classes_cache
        classes: list[type[BaseException]] = [
            asyncio.TimeoutError, TimeoutError, ConnectionError,
        ]
        for module_name, names in (
            ("anthropic", ("APIConnectionError", "APITimeoutError",
                           "RateLimitError", "InternalServerError")),
            ("openai", ("APIConnectionError", "APITimeoutError",
                        "RateLimitError", "InternalServerError")),
        ):
            try:
                module = __import__(module_name)
            except ImportError:
                continue
            for name in names:
                klass = getattr(module, name, None)
                if isinstance(klass, type) and issubclass(klass, BaseException):
                    classes.append(klass)
        cls._retryable_llm_classes_cache = tuple(classes)
        return cls._retryable_llm_classes_cache

    @classmethod
    def _is_llm_retryable(cls, exc: Exception) -> bool:
        """Return True for transient errors worth retrying.

        Primary: isinstance against known SDK error classes (anthropic / openai).
        Fallback: match unambiguous HTTP-error tokens for third-party providers
        whose errors don't inherit from those SDK classes (DeepSeek, Ollama, etc.).
        Avoids matching ambiguous words like "timeout" or "connection" that
        can appear inside non-transient error messages.
        """
        if isinstance(exc, cls._retryable_llm_classes()):
            return True
        error_msg = str(exc).lower()
        retryable_tokens = (
            " 429", " 500", " 502", " 503", " 504",
            "rate limit", "too many requests",
            "service unavailable", "overloaded",
        )
        return any(token in error_msg for token in retryable_tokens)

    @staticmethod
    def _is_content_filter_block(exc: Exception) -> bool:
        """Return True when the provider rejected the request due to content policy."""
        error_msg = str(exc).lower()
        return "content exists risk" in error_msg or "content filter" in error_msg

    @staticmethod
    def _build_summarized_tool_results(
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

    async def _recover_from_content_filter(
        self,
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
                await self.content_filter.learn_and_persist([results[0]])
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
        if tool_uses:
            # The transport knows how many trailing tool-result messages it
            # appended for this batch; +1 for the preceding assistant turn.
            result_msg_count = self._transport.tool_result_rollback_count(len(tool_uses))
            del ctx.messages[-(result_msg_count + 1):]

        # Append a summarized user message instead
        if tool_uses and results:
            summary = self._build_summarized_tool_results(tool_uses, results)
            ctx.messages.append({"role": "user", "content": summary})
            logger.info(
                "Content filter recovery: rolled back %d messages, "
                "replaced with summarized tool results",
                messages_before - len(ctx.messages) + 1,
            )

        # Retry once with the cleaned messages
        try:
            tools = self.registry.to_anthropic_format() if ctx.tools_enabled else []
            return await self._create(ctx, tools)
        except Exception as retry_exc:
            if self._is_content_filter_block(retry_exc):
                logger.warning(
                    "Content filter recovery retry also blocked — "
                    "summarized results still triggered filter"
                )
            else:
                logger.warning(
                    "Content filter recovery retry failed: %s", retry_exc
                )
            return None

    async def _with_llm_retry(self, fn, *args, **kwargs):
        """Call *fn* with retry on transient LLM API errors."""
        last_exc = None
        for attempt in range(self.llm_max_retries + 1):
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                if attempt >= self.llm_max_retries or not self._is_llm_retryable(exc):
                    raise
                delay = self.llm_retry_base_delay * (2 ** attempt)
                logger.warning(
                    "LLM API error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, self.llm_max_retries, delay, exc,
                )
                await asyncio.sleep(delay)
        raise last_exc  # type: ignore[misc]

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

    @staticmethod
    def _synthesize_tool_only_response(
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

    @staticmethod
    def _stable_tool_loop_key(value: Any) -> str:
        try:
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            raw = repr(value)
        if len(raw) <= 600:
            return raw
        return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()

    @classmethod
    def _tool_use_signature(cls, tool_uses: list[dict]) -> str:
        return cls._stable_tool_loop_key(
            [
                {
                    "name": str(tool_use.get("name", "")),
                    "input": tool_use.get("input", {}),
                }
                for tool_use in tool_uses
            ]
        )

    # Fields whose values are typically large or vary across otherwise-identical
    # calls (file content, search hits, raw bodies, base64 blobs, random ids,
    # timestamps).  Including any of these in a tool-loop signature would
    # either bloat it or — for varying values — make every call's signature
    # unique and blind stuck-detection to genuine loops.
    _SIGNATURE_NOISY_KEYS = frozenset({
        # Bulky payloads
        "content", "text", "body", "data", "bytes", "raw", "raw_bytes",
        "preview", "summary_text", "full_content", "result_preview",
        # Free-running identifiers / timestamps the agent does not control
        "timestamp", "elapsed", "elapsed_ms", "elapsed_seconds",
        "duration_ms", "duration_seconds",
    })
    # Suffixes that mark a field as a varying identifier or timestamp
    # (e.g. ``task_id``, ``next_run_at``, ``started_at``, ``created_time``).
    _SIGNATURE_NOISY_SUFFIXES = ("_id", "_at", "_time", "_ms", "_ns", "_us",
                                 "_timestamp", "_uuid")
    _SIGNATURE_VALUE_LIMIT = 200

    @classmethod
    def _signature_keep(cls, key: str) -> bool:
        if key in cls._SIGNATURE_NOISY_KEYS:
            return False
        return not any(key.endswith(suffix) for suffix in cls._SIGNATURE_NOISY_SUFFIXES)

    @classmethod
    def _signature_value(cls, value: Any) -> Any:
        """Shrink large values so they don't dominate a tool-loop signature."""
        if isinstance(value, str) and len(value) > cls._SIGNATURE_VALUE_LIMIT:
            return {"truncated": True, "len": len(value)}
        if isinstance(value, (list, tuple)) and len(value) > 16:
            return {"truncated": True, "count": len(value)}
        return value

    @classmethod
    def _normalized_tool_result(cls, raw_result: str) -> Any:
        """Strip noisy fields and cap large values so equal results compare equal.

        Deny-list rather than allow-list: any structured field a tool adds
        in the future is automatically included in dedup signatures unless
        it matches a known noisy key or noisy-suffix pattern.
        """
        text = str(raw_result or "").strip()
        if not text:
            return {"empty": True}
        try:
            payload = json.loads(text)
        except Exception:
            return text[:600]
        if not isinstance(payload, dict):
            return payload
        return {
            key: cls._signature_value(value)
            for key, value in payload.items()
            if cls._signature_keep(key)
        } or payload

    @classmethod
    def _tool_result_signature(cls, results: list[str]) -> str:
        return cls._stable_tool_loop_key(
            [cls._normalized_tool_result(result) for result in results]
        )

    @staticmethod
    def _tool_result_looks_unproductive(raw_result: str) -> bool:
        text = str(raw_result or "").strip()
        if not text:
            return True
        try:
            payload = json.loads(text)
        except Exception:
            lower = text.lower()
            return any(
                marker in lower
                for marker in (
                    "timed out",
                    "timeout",
                    "not found",
                    "permission denied",
                    "requires confirmation",
                    "blocked",
                    "error",
                )
            )
        if not isinstance(payload, dict):
            return False
        # First-principles: a tool that explicitly signals "this is a soft
        # error and you can recover" (via `recoverable_by_agent: true` or by
        # attaching a non-empty `recovery_hint`) is NOT an unproductive result
        # — the tool author has guaranteed the agent has enough information
        # to adjust.  Treating those as failures collapses the entire grace
        # the recovery_hint protocol is meant to provide and trips the
        # watchdog after only 3-4 honest retries.
        # `same_pair` (literally identical input + result) still catches the
        # case where the agent ignores the hint and repeats verbatim.
        if payload.get("recoverable_by_agent") is True:
            return False
        if payload.get("recovery_hint"):
            return False
        if payload.get("requires_confirmation") is True:
            return True
        if payload.get("blocked") is True:
            return True
        if payload.get("ok") is False:
            return True
        if payload.get("error") and payload.get("ok") is not True:
            return True
        meaningful_values = [
            value for value in payload.values() if value not in (None, "", [], {})
        ]
        return not meaningful_values

    @classmethod
    def _tool_results_look_unproductive(cls, results: list[str]) -> bool:
        if not results:
            return True
        return all(cls._tool_result_looks_unproductive(result) for result in results)

    @staticmethod
    def _tool_results_are_intent_required(results: list[str]) -> bool:
        if not results:
            return False
        for raw_result in results:
            raw_text = str(raw_result or "")
            try:
                payload = json.loads(raw_text)
            except Exception:
                lower = raw_text.lower()
                if "intent required" in lower or "intent declaration too vague" in lower:
                    continue
                return False
            if isinstance(payload, dict) and payload.get("intent_required") is True:
                continue
            if (
                isinstance(payload, dict)
                and "intent" in str(payload.get("error", "")).lower()
            ):
                continue
            return False
        return True

    _looks_like_chinese = staticmethod(shared._looks_like_chinese)

    @classmethod
    def _tool_result_preview(cls, results: list[str], limit: int = 220) -> str:
        previews: list[str] = []
        for raw_result in results[:3]:
            normalized = cls._normalized_tool_result(raw_result)
            if isinstance(normalized, dict):
                for key in (
                    "recovery_hint",
                    "error",
                    "reason",
                    "message",
                    "status",
                    "stderr",
                    "stdout",
                ):
                    value = normalized.get(key)
                    if value:
                        previews.append(_preview_text(value, limit=limit))
                        break
                else:
                    previews.append(_preview_text(normalized, limit=limit))
            else:
                previews.append(_preview_text(normalized, limit=limit))
        return " | ".join(previews)[:limit]

    @classmethod
    def _build_tool_loop_stuck_response(
        cls,
        *,
        user_message: str,
        reason: str,
        tool_uses: list[dict],
        results: list[str],
    ) -> str:
        tool_names = ", ".join(str(tool_use.get("name", "")) for tool_use in tool_uses)
        result_preview = cls._tool_result_preview(results)
        if cls._looks_like_chinese(user_message):
            if reason == "intent_required":
                return "\n".join(
                    [
                        (
                            "我先停一下：这次卡住的不是路径、权限或下载问题，"
                            "而是内部工具安全规则没有被满足。"
                        ),
                        "",
                        (
                            f"当前观察：模型连续尝试调用 `{tool_names or 'unknown'}`，"
                            "但 shell 调用没有带合格的结构化 `intent`，所以工具被安全拦截。"
                        ),
                        "",
                        "合理的下一步是重新规划这一步：每个 shell 调用都应在 `intent` 里说明命令会做什么、为什么需要运行；如果目标本身不明确，我应该先向你确认，而不是继续空转。",
                    ]
                )
            reason_text = {
                "same_pair": "同一个工具请求连续产生相同结果",
                "same_result": "工具结果重复且没有新信息",
                "unproductive_rounds": "连续多轮工具调用失败或没有有效输出",
                "same_batch_unproductive": "同一批工具持续返回无效结果",
            }.get(reason, reason)
            lines = [
                (
                    "我先停一下：我检测到本轮已经连续多次调用工具，"
                    "但结果没有继续推进；继续自动尝试大概率只会耗到系统上限。"
                ),
                "",
                (
                    f"当前观察：最近一轮工具是 `{tool_names or 'unknown'}`；"
                    f"触发原因是 {reason_text}。"
                ),
            ]
            if result_preview:
                lines.append(f"最近结果：{result_preview}")
            lines.extend(
                [
                    "",
                    "你可以直接回复选择下一步：",
                    "1. 提供新的路径、权限、凭据或搜索方向后继续",
                    "2. 让我基于已有结果先总结",
                    "3. 告诉我该在几个可选方案里选哪个",
                    "4. 停止这个任务",
                ]
            )
            return "\n".join(lines)

        if reason == "intent_required":
            return "\n".join(
                [
                    (
                        "I am pausing because the loop is blocked by the internal "
                        "tool safety protocol, not by a missing path, permission, "
                        "or credential."
                    ),
                    "",
                    (
                        f"Current signal: the model repeatedly tried to call "
                        f"`{tool_names or 'unknown'}` without a specific structured "
                        "`intent` input, so the tool calls were blocked."
                    ),
                    "",
                    (
                        "The right next step is to re-plan this action: each shell "
                        "call should include an `intent` explaining what the command "
                        "does and why it is needed, or ask you for clarification if "
                        "the goal is ambiguous."
                    ),
                ]
            )
        reason_text = {
            "same_pair": "the same tool request produced the same result repeatedly",
            "same_result": "the tool results repeated without new information",
            "unproductive_rounds": (
                "several consecutive tool rounds failed or returned no useful output"
            ),
            "same_batch_unproductive": (
                "the same tool batch kept returning unproductive results"
            ),
        }.get(reason, reason)
        lines = [
            (
                "I am pausing here because the tool loop is no longer making "
                "clear progress; continuing automatically would likely just "
                "hit the hard iteration limit."
            ),
            "",
            (
                f"Current signal: latest tool batch was `{tool_names or 'unknown'}`; "
                f"trigger was {reason_text}."
            ),
        ]
        if result_preview:
            lines.append(f"Latest result: {result_preview}")
        lines.extend(
            [
                "",
                "Please choose the next step:",
                "1. Continue with a new path, permission, credential, or search direction",
                "2. Summarize what we have so far",
                "3. Tell me which option to pick",
                "4. Stop this task",
            ]
        )
        return "\n".join(lines)

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
            regular_executor = RegularToolExecutor(
                self.registry,
                plugin_catalog=self.plugin_catalog,
            )
            # D2: return_exceptions=True preserves successes when one tool errors
            raw = await asyncio.gather(
                *[regular_executor.run(tu) for _, tu in regular_calls],
                return_exceptions=True,
            )
            for (idx, tu), outcome in zip(regular_calls, raw):
                if isinstance(outcome, BaseException):
                    results[idx] = json.dumps(
                        {"ok": False, "error": f"tool '{tu['name']}' raised: {outcome}"}
                    )
                else:
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
                raw_spawn, runtime_metrics = await self._run_orchestrated_spawn_calls(
                    spawn_calls,
                    orchestration_decision
                    or OrchestrationDecision(mode="explicit"),
                )
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

    @staticmethod
    def _new_watchdog_state() -> dict[str, Any]:
        """Per-turn dedup state for tool-loop stuck-detection."""
        return {
            "last_pair_sig": "",
            "last_result_sig": "",
            "same_pair": 0,
            "same_result": 0,
            "unproductive_rounds": 0,
            "batch_counts": {},
        }

    def _check_tool_loop_stuck(
        self,
        state: dict[str, Any],
        tool_uses: list[dict],
        results: list[str],
    ) -> str:
        """Update watchdog state and return a stuck-reason string, or ''.

        Pure single-pass over the new batch:
        - compute batch + result signatures
        - update run-length counters and per-signature occurrence counts
        - test the threshold rules in priority order

        Extracted from send_message so the body of the tool loop reads as
        intent (a function call) instead of mechanism (60 lines of counters).
        """
        batch_sig = self._tool_use_signature(tool_uses)
        result_sig = self._tool_result_signature(results)
        pair_sig = (batch_sig, result_sig)

        state["batch_counts"][batch_sig] = state["batch_counts"].get(batch_sig, 0) + 1

        if pair_sig == state["last_pair_sig"]:
            state["same_pair"] += 1
        else:
            state["same_pair"] = 1
            state["last_pair_sig"] = pair_sig
        if result_sig == state["last_result_sig"]:
            state["same_result"] += 1
        else:
            state["same_result"] = 1
            state["last_result_sig"] = result_sig

        unproductive = self._tool_results_look_unproductive(results)
        intent_required = self._tool_results_are_intent_required(results)
        if unproductive or state["same_pair"] >= self._TOOL_LOOP_REPEAT_THRESHOLD:
            state["unproductive_rounds"] += 1
        else:
            state["unproductive_rounds"] = 0

        if intent_required and state["same_result"] >= self._TOOL_LOOP_REPEAT_THRESHOLD:
            return "intent_required"
        if state["same_pair"] >= self._TOOL_LOOP_REPEAT_THRESHOLD:
            return "same_pair"
        if unproductive and state["same_result"] >= self._TOOL_LOOP_REPEAT_THRESHOLD:
            return "same_result"
        if state["unproductive_rounds"] >= self._TOOL_LOOP_UNPRODUCTIVE_THRESHOLD:
            return "unproductive_rounds"
        if unproductive and state["batch_counts"][batch_sig] >= self._TOOL_LOOP_UNPRODUCTIVE_THRESHOLD:
            return "same_batch_unproductive"
        return ""

    @staticmethod
    def _inject_pending_interjections(
        ctx: "AgentContext", pending: list[dict]
    ) -> None:
        """Drain the per-session mailbox into ctx.messages as a user_interjection.

        ``pending`` is shared mutable state owned by RuntimeSessionState — we
        list-copy then ``clear()`` in place so the channel handler's
        subsequent appends only see fresh entries.  Each entry contributes
        one ``<user_interjection>`` block; an instruction footer tells the
        LLM how to handle it (continue, adjust, summarize, stop).
        """
        drained = list(pending)
        pending.clear()
        if not drained:
            return
        blocks: list[str] = []
        for entry in drained:
            text = str(entry.get("text", "") or "").strip()
            if not text:
                continue
            who = str(entry.get("from_user", "") or "user")
            urgency = str(entry.get("urgency", "normal") or "normal")
            arrived = entry.get("arrived_at")
            when = ""
            if isinstance(arrived, (int, float)):
                from datetime import datetime as _dt
                when = _dt.fromtimestamp(arrived).strftime("%H:%M:%S")
            attrs = f' from="{who}" urgency="{urgency}"'
            if when:
                attrs += f' at="{when}"'
            blocks.append(f"<user_interjection{attrs}>\n{text}\n</user_interjection>")
        if not blocks:
            return
        footer = (
            "\n\nThe user interjected during your in-progress task. "
            "Briefly acknowledge what you heard, then decide: continue / "
            "adjust direction / pause and ask for clarification / stop and "
            "summarize what you have so far.  Treat urgency=\"now\" as "
            "explicit interruption (read and respond first); urgency="
            "\"normal\" as info added in passing (acknowledge and "
            "incorporate when sensible)."
        )
        ctx.messages.append({
            "role": "user",
            "content": "\n\n".join(blocks) + footer,
        })

    def _prepare_turn(
        self,
        ctx: "AgentContext",
        user_message: str,
        attachments: tuple[MessageAttachment, ...],
    ) -> OrchestrationDecision:
        """Mutate ctx for one turn: inject LTM context, activate skills, plan
        orchestration, and append the user message.  Returns the orchestration
        decision so the loop can route spawn calls correctly.

        Caller is responsible for restoring ``ctx.system_prompt`` afterwards
        (send_message captures ``original_system`` before calling this).
        """
        # retrieve_implicit_context() covers both recent staging-buffer turns
        # (not yet consolidated) and historical LTM hits.  Skipping the
        # staging side would let recently-compacted turns drop from view.
        if self.context_manager:
            retrieved = self.context_manager.retrieve_implicit_context(
                user_message,
                current_messages=ctx.messages,
            )
            if retrieved:
                ctx.system_prompt = ctx.system_prompt + "\n\n" + retrieved
        skill_catalog: Optional[SkillCatalog] = ctx.metadata.get("skill_catalog")
        required_skills: list[str] = list(ctx.metadata.get("required_skills", []))
        if skill_catalog and required_skills:
            active_blocks = []
            for skill_ref in required_skills:
                activation = skill_catalog.activation_text(skill_ref, explicit=True)
                if activation:
                    active_blocks.append(activation)
            if active_blocks:
                ctx.system_prompt = (
                    ctx.system_prompt
                    + "\n\n## Active Skills\n"
                    + "\n\n".join(active_blocks)
                )
        decision = self._plan_orchestration(ctx, user_message)
        ctx.messages.append(
            {
                "role": "user",
                "content": self._build_user_message_content(user_message, attachments),
            }
        )
        return decision

    async def _handle_end_turn(
        self,
        ctx: "AgentContext",
        response: Any,
        text: str,
        streamed_text: str,
        prior_text: str,
        tool_result_history: list[tuple[str, str]],
    ) -> tuple[str, Optional[str]]:
        """Finalize a non-tool-use response and return (result_text, continuation_error).

        Picks the best text (parsed > streamed > prior), promotes a tool's
        ``summary_text`` if the model returned nothing, appends the assistant
        entry, attempts truncation continuation when the transport reports
        one, and falls back to an apology when the response is entirely
        empty.  ``continuation_error`` is non-None only when continuation
        still failed; caller propagates it.
        """
        result_text = text or streamed_text or prior_text
        if not result_text and tool_result_history:
            result_text = self._synthesize_tool_only_response(tool_result_history)
        ctx.messages.append(self._transport.build_final_message(response, result_text))

        completion_error = self._response_completion_error(response)
        if completion_error:
            result_text, continuation_error = (
                await self._continue_truncated_response(ctx, result_text)
            )
            ctx.messages[-1] = self._transport.build_final_message(
                response, result_text
            )
            return result_text, continuation_error

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
        return result_text, None

    async def send_message(
        self,
        ctx: "AgentContext",
        user_message: str,
        stream_callback: Optional[Callable[[str], None]] = None,
        attachments: tuple[MessageAttachment, ...] = (),
    ) -> "AgentResult":
        # Capture original system prompt before any per-turn injections.
        original_system = ctx.system_prompt
        tool_calls_made: list[str] = []
        tool_result_history: list[tuple[str, str]] = []
        result_text = ""
        watchdog = self._new_watchdog_state()
        # Per-turn heartbeat state — updated as we move between LLM calls,
        # tool batches, and sub-agent dispatch.  A background task reads
        # this every few seconds and fires sink.on_heartbeat so live UIs
        # (Feishu cards) can show "agent is alive, elapsed N seconds on X".
        heartbeat_state: dict[str, Any] = {
            "op": "starting",
            "detail": "",
            "started_at": time.monotonic(),
            "active": True,
            "current_tool": None,
        }
        content_filter_recovered_response: Any | None = None
        content_filter_submitted_tool_uses: list[dict] | None = None
        content_filter_submitted_results: list[str] | None = None
        turn_started_at = time.perf_counter()
        trace_status = "ok"
        trace_error: str | None = None
        trace_iterations = 0
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

        # B1: wrap ALL mutations (prompt injection, messages append, stack push)
        # inside the try/finally so they are always cleaned up on error.
        # Start the per-turn heartbeat task here so it sees the active sink.
        from agent.core.output import _active_sink as _hb_sink_var
        _hb_interval = float(ctx.metadata.get("heartbeat_interval", 5.0) or 5.0)
        if _hb_interval <= 0:
            _hb_interval = 5.0
        heartbeat_writer: HeartbeatWriter | None = None
        if ctx.metadata.get("heartbeat_enabled", True) is not False:
            heartbeat_writer = HeartbeatWriter(
                session_id=str(ctx.metadata.get("session_id") or ctx.agent_id),
                agent_id=ctx.agent_id,
                path=ctx.metadata.get("heartbeat_path"),
            )

        def _write_runtime_heartbeat(*, status: str = "running", active: bool = True) -> None:
            if heartbeat_writer is None:
                return
            pending = ctx.metadata.get("pending_messages") or []
            with shared._suppress_with_log("heartbeat writer failed"):
                heartbeat_writer.write(
                    state=str(heartbeat_state["op"]),
                    detail=str(heartbeat_state["detail"]),
                    current_tool=heartbeat_state.get("current_tool"),
                    turn_id=str(ctx.metadata.get("turn_id") or ""),
                    pending_messages=len(pending),
                    active=active,
                    status=status,
                )

        async def _heartbeat_tick() -> None:
            _write_runtime_heartbeat()
            while heartbeat_state["active"]:
                try:
                    await asyncio.sleep(_hb_interval)
                except asyncio.CancelledError:
                    return
                if not heartbeat_state["active"]:
                    return
                _write_runtime_heartbeat()
                sink = _hb_sink_var.get()
                if sink is None:
                    continue
                fn = getattr(sink, "on_heartbeat", None)
                if not callable(fn):
                    continue
                elapsed = max(0.0, time.monotonic() - float(heartbeat_state["started_at"]))
                # Don't tick for super-fast ops — most LLM calls finish in <5s
                # and the first tick would land right as we're handing back.
                if elapsed < _hb_interval - 0.5:
                    continue
                pending = ctx.metadata.get("pending_messages") or []
                with shared._suppress_with_log("sink.on_heartbeat raised"):
                    fn(
                        elapsed_seconds=elapsed,
                        current_op=str(heartbeat_state["op"]),
                        op_detail=str(heartbeat_state["detail"]),
                        pending_messages=len(pending),
                    )

        _heartbeat_task = asyncio.create_task(_heartbeat_tick())

        try:
            orchestration_decision = self._prepare_turn(ctx, user_message, attachments)

            # D1: bounded tool-call loop — prevents infinite model loops
            max_tool_call_iterations = max(1, int(self.max_tool_call_iterations))
            _iteration = 0
            while True:
                trace_iterations = _iteration + 1
                if _iteration == max_tool_call_iterations:
                    trace_status = "tool_loop_exceeded"
                    trace_error = (
                        f"Tool-call loop exceeded {max_tool_call_iterations} "
                        "iterations; possible model loop detected."
                    )
                    return AgentResult(
                        agent_id=ctx.agent_id,
                        content=result_text,
                        tool_calls_made=tool_calls_made,
                        error=trace_error,
                    )
                tools = self.registry.to_anthropic_format() if ctx.tools_enabled else []

                # Drain the interjection mailbox: any user messages that
                # arrived during the previous step get folded in as a
                # <user_interjection> block so the next LLM call sees them
                # alongside the original task.  Channel handler appends to
                # this list; we drain in place so future iterations see
                # only fresh entries.  Sub-agents have no mailbox (key is
                # absent), so this is a no-op for them.
                pending = ctx.metadata.get("pending_messages")
                if pending:
                    self._inject_pending_interjections(ctx, pending)

                # Cooperative cancellation: check at every tool-loop boundary
                # so the running turn can be interrupted cleanly without
                # orphaning subprocesses or losing the turn record.
                cancel_token = ctx.metadata.get("cancel_token")
                if cancel_token is not None and cancel_token.is_cancelled:
                    trace_status = "cancelled"
                    trace_error = "Turn cancelled by user"
                    return AgentResult(
                        agent_id=ctx.agent_id,
                        content=result_text or shared._cancelled_by_user_text(user_message),
                        tool_calls_made=tool_calls_made,
                        error=trace_error,
                    )

                # Keep working memory bounded for sub-agents (the main
                # interactive loop compacts between turns, but sub-agents
                # only call send_message() once and may hit the token limit
                # during long tool-call sequences).
                if (
                    _iteration > 0
                    and self.context_manager
                    and self.context_manager.should_compact_messages(
                        ctx.messages, self.max_tokens
                    )
                ):
                    ctx.messages = self.context_manager.compact_messages(
                        ctx.messages
                    )

                try:
                    response_started_at = time.perf_counter()
                    heartbeat_state["op"] = "LLM"
                    heartbeat_state["detail"] = self.model
                    heartbeat_state["started_at"] = time.monotonic()
                    heartbeat_state["current_tool"] = None
                    if heartbeat_writer is not None:
                        heartbeat_writer.mark_progress()
                    _write_runtime_heartbeat()
                    if content_filter_recovered_response is not None:
                        response = content_filter_recovered_response
                        content_filter_recovered_response = None
                        streamed_text = ""
                    else:
                        # Wrap the LLM call as a task and register a cleanup
                        # so /now (force-cancel) aborts the HTTP request mid-flight.
                        # Graceful cancel waits for the call to finish naturally.
                        if stream_callback:
                            llm_task = asyncio.create_task(
                                self._with_llm_retry(
                                    self._stream_response, ctx, tools, stream_callback
                                )
                            )
                        else:
                            llm_task = asyncio.create_task(
                                self._with_llm_retry(self._create, ctx, tools)
                            )

                        def _abort_llm(level: str) -> None:
                            if level == "force":
                                llm_task.cancel()

                        active_token = shared._active_cancel_token.get()
                        _llm_deregister = (
                            active_token.register_cleanup("llm_call", _abort_llm)
                            if active_token is not None
                            else (lambda: None)
                        )
                        try:
                            llm_result = await llm_task
                        finally:
                            _llm_deregister()
                        if stream_callback:
                            response, streamed_text = llm_result
                        else:
                            response = llm_result
                            streamed_text = ""
                    content_filter_submitted_tool_uses = None
                    content_filter_submitted_results = None
                    stop_reason, text, tool_uses = self._parse_response(response)
                    _trace_latency(
                        "model_response_received",
                        agent_id=ctx.agent_id,
                        iteration=_iteration + 1,
                        stop_reason=stop_reason,
                        tool_uses=len(tool_uses),
                        duration_ms=f"{(time.perf_counter() - response_started_at) * 1000:.1f}",
                    )
                    if tool_uses:
                        _interaction_log(
                            "tool_batch_requested",
                            agent_id=ctx.agent_id,
                            iteration=_iteration + 1,
                            stop_reason=stop_reason,
                            tool_uses=len(tool_uses),
                            tool_names=",".join(tu["name"] for tu in tool_uses[:5]),
                        )

                    if stop_reason == "tool_use" and tool_uses:
                        if text:
                            result_text = text
                        ctx.messages.append(self._assistant_message(response, text))

                        # Set intent context so tool executors can enforce
                        # the intent-before-action protocol.
                        import agent.core.output as _out

                        _intent_token = _out._active_assistant_text.set(
                            text or streamed_text or ""
                        )
                        tool_calls_made.extend(tu["name"] for tu in tool_uses)
                        tool_use_started_at = time.perf_counter()
                        heartbeat_state["op"] = "tools"
                        heartbeat_state["detail"] = ", ".join(
                            tu["name"] for tu in tool_uses[:3]
                        ) + (f" +{len(tool_uses) - 3}" if len(tool_uses) > 3 else "")
                        heartbeat_state["started_at"] = time.monotonic()
                        heartbeat_state["current_tool"] = heartbeat_state["detail"]
                        if heartbeat_writer is not None:
                            heartbeat_writer.mark_progress()
                        _write_runtime_heartbeat()
                        try:
                            results = await self._run_tool_uses(
                                tool_uses,
                                orchestration_decision=orchestration_decision,
                            )
                        finally:
                            _out._active_assistant_text.reset(_intent_token)
                        _trace_latency(
                            "tool_uses_finished",
                            agent_id=ctx.agent_id,
                            iteration=_iteration + 1,
                            total_tool_uses=len(tool_uses),
                            spawn_calls=sum(
                                1 for tool_use in tool_uses
                                if "orchestration" in self.registry.tool_capabilities(tool_use["name"])
                            ),
                            regular_calls=sum(
                                1 for tool_use in tool_uses
                                if "orchestration" not in self.registry.tool_capabilities(tool_use["name"])
                            ),
                            duration_ms=f"{(time.perf_counter() - tool_use_started_at) * 1000:.1f}",
                        )
                        _interaction_log(
                            "tool_batch_finished",
                            agent_id=ctx.agent_id,
                            iteration=_iteration + 1,
                            tool_uses=len(tool_uses),
                            duration_ms=f"{(time.perf_counter() - tool_use_started_at) * 1000:.1f}",
                        )
                        tool_result_history.extend(
                            (tu["name"], self._extract_summary_text(res))
                            for tu, res in zip(tool_uses, results)
                        )
                        # Content filter: screen tool results before API submission.
                        # Flagged results get summarized to avoid triggering provider
                        # content policy filters on subsequent API calls.
                        filtered_results, risky_indices = filter_tool_results(
                            self.content_filter,
                            tool_uses,
                            results,
                            threshold=self._content_filter_threshold,
                        )
                        if risky_indices:
                            await self.content_filter.learn_and_persist(
                                [results[i] for i in risky_indices]
                            )
                            logger.info(
                                "Content filter flagged %d/%d tool results as risky: %s",
                                len(risky_indices), len(results),
                                [tool_uses[i]["name"] for i in risky_indices],
                            )
                        restart_message = self._clear_context_restart_message(
                            tool_uses, results
                        )
                        if restart_message is not None:
                            ctx.messages[:] = [
                                {"role": "user", "content": restart_message}
                            ]
                            content_filter_submitted_tool_uses = None
                            content_filter_submitted_results = None
                            _iteration += 1
                            continue

                        ctx.messages.extend(
                            self._tool_result_messages(tool_uses, filtered_results)
                        )
                        content_filter_submitted_tool_uses = list(tool_uses)
                        content_filter_submitted_results = list(results)

                        stuck_reason = self._check_tool_loop_stuck(
                            watchdog, tool_uses, results,
                        )
                        if stuck_reason:
                            result_text = self._build_tool_loop_stuck_response(
                                user_message=user_message,
                                reason=stuck_reason,
                                tool_uses=tool_uses,
                                results=results,
                            )
                            ctx.messages.append(
                                {"role": "assistant", "content": result_text}
                            )
                            trace_status = "tool_loop_stuck"
                            _interaction_log(
                                "tool_loop_watchdog_stopped",
                                agent_id=ctx.agent_id,
                                iteration=_iteration + 1,
                                reason=stuck_reason,
                                tool_uses=len(tool_uses),
                                tool_names=",".join(
                                    tu["name"] for tu in tool_uses[:5]
                                ),
                            )
                            return AgentResult(
                                agent_id=ctx.agent_id,
                                content=result_text,
                                tool_calls_made=tool_calls_made,
                            )
                        _iteration += 1
                        continue
                    else:
                        result_text, continuation_error = await self._handle_end_turn(
                            ctx, response, text, streamed_text,
                            result_text, tool_result_history,
                        )
                        if continuation_error is not None:
                            trace_status = "continuation_error"
                            trace_error = continuation_error
                            return AgentResult(
                                agent_id=ctx.agent_id,
                                content=result_text,
                                tool_calls_made=tool_calls_made,
                                error=continuation_error,
                            )
                        break

                except Exception as e:
                    if self._is_content_filter_block(e):
                        # Recovery: rollback last assistant+tool_result messages,
                        # summarize the blocked content, and retry.
                        recovered_response = await self._recover_from_content_filter(
                            ctx,
                            content_filter_submitted_tool_uses,
                            content_filter_submitted_results,
                        )
                        if recovered_response is not None:
                            content_filter_recovered_response = recovered_response
                            content_filter_submitted_tool_uses = None
                            content_filter_submitted_results = None
                            trace_status = "content_filter_recovered"
                            _interaction_log(
                                "content_filter_recovered",
                                agent_id=ctx.agent_id,
                                iteration=_iteration + 1,
                            )
                            continue
                        # Recovery failed — let the error propagate
                        trace_status = "content_filter_unrecoverable"
                        trace_error = (
                            "Content filter blocked the request and recovery failed. "
                            "Consider switching to a different provider "
                            "(e.g. /model anthropic or /model openai) for "
                            "this task."
                        )
                        result_text = trace_error
                        ctx.messages.append(
                            {"role": "assistant", "content": trace_error}
                        )
                        break

                    trace_status = "exception"
                    trace_error = self._format_agent_error(e)
                    return AgentResult(
                        agent_id=ctx.agent_id,
                        content="",
                        tool_calls_made=tool_calls_made,
                        error=trace_error,
                    )
        finally:
            _active_agent_context.reset(_active_ctx_token)
            shared._active_cancel_token.reset(_cancel_token_token)
            heartbeat_state["op"] = "finished"
            heartbeat_state["detail"] = trace_status
            heartbeat_state["current_tool"] = None
            _write_runtime_heartbeat(
                status=trace_status if trace_status != "ok" else "finished",
                active=False,
            )
            heartbeat_state["active"] = False
            _heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await _heartbeat_task
            _interaction_log(
                "turn_finished",
                agent_id=ctx.agent_id,
                status=trace_status,
                error=trace_error,
                iterations=trace_iterations,
                tool_calls=len(tool_calls_made),
                content_len=len(result_text),
                content_preview=_preview_text(result_text),
                duration_ms=f"{(time.perf_counter() - turn_started_at) * 1000:.1f}",
            )
            _trace_latency(
                "send_message_finished",
                agent_id=ctx.agent_id,
                status=trace_status,
                error=trace_error,
                iterations=trace_iterations,
                tool_calls=len(tool_calls_made),
                duration_ms=f"{(time.perf_counter() - turn_started_at) * 1000:.1f}",
            )
            # Always restore the original system prompt.  The current ctx
            # is published via the _active_agent_context ContextVar; that
            # token is reset above, so no extra stack bookkeeping is needed.
            ctx.system_prompt = original_system

        return AgentResult(
            agent_id=ctx.agent_id,
            content=result_text,
            tool_calls_made=tool_calls_made,
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
        if ctx_mgr:
            if record_kwargs is None:
                record_kwargs = {}
            ctx_mgr.record_turn(
                user_content=user_content,
                assistant_content=assistant_content or "",
                channel=channel,
                **record_kwargs,
            )
            record_runtime_event = getattr(ctx_mgr, "record_runtime_event", None)
            if callable(record_runtime_event):
                record_runtime_event(
                    "turn_finished",
                    {
                        "user_content": user_content,
                        "assistant_content": assistant_content or "",
                        "error": error or "",
                        "channel": channel,
                        "metadata": record_kwargs,
                    },
                )
            ctx_mgr.staging.append("user", user_content)
            if assistant_content:
                ctx_mgr.staging.append("assistant", assistant_content)
            if ctx_mgr.should_enqueue_consolidation():
                ctx_mgr.enqueue_consolidation("staged_turns")
        # Keep working memory bounded without blocking on LLM consolidation.
        if ctx_mgr and ctx_mgr.should_compact_messages(
            ctx.messages, agent.max_tokens
        ):
            ctx.messages = ctx_mgr.compact_messages(ctx.messages)
            if system_prompt:
                ctx.system_prompt = agent_module._with_task_context(
                    system_prompt, task_context
                )
            if (
                memory_worker is not None
                and ctx_mgr.staging.count() >= ctx_mgr.min_messages
            ):
                ctx_mgr.enqueue_consolidation("compact_triggered")
                memory_worker.wake()

    async def _stream_response(
        self,
        ctx: "AgentContext",
        tools: list[dict],
        callback: Callable[[str], Any],
    ) -> tuple[Any, str]:
        """Stream response and return (full_response, collected_text).

        ``callback`` may be a plain sync function or an async coroutine
        function; the transport handles both.
        """
        return await self._transport.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            system=ctx.system_prompt,
            messages=ctx.messages,
            tools=tools,
            callback=callback,
        )

    # ── Sub-agent construction helpers (used by _execute_agent and
    # register_spawn_capability) ───────────────────────────────────────

    def _create_sub_registry(
        self,
        capability_profile: str = "full",
        write_scope: list[str] | None = None,
    ) -> "ToolRegistry":
        # Snapshot the registry to avoid RuntimeError if tools are added
        # concurrently (e.g. via /generate-tool while a spawn batch runs).
        tools_snapshot = dict(self.registry._tools)
        sub_registry = ToolRegistry(console=shared.CONSOLE)
        allowed_capabilities: set[str] | None = None
        if capability_profile in {"read_only", "research"}:
            allowed_capabilities = {"read"}
        elif capability_profile == "implementation":
            allowed_capabilities = {"read"}
            if write_scope:
                allowed_capabilities.add("workspace_write")
        for name, tool_def in tools_snapshot.items():
            # Sub-agents must never spawn further sub-agents — exclude any
            # orchestration-capable tool by capability, not by name.
            if "orchestration" in tool_def.capabilities:
                continue
            if allowed_capabilities is not None:
                if not tool_def.capabilities:
                    continue
                if not tool_def.capabilities.issubset(allowed_capabilities):
                    continue
            sub_registry._tools[name] = tool_def
        # Copy the context dict, cloning only the mutable containers
        # (shell_blocked_commands list) rather than deep-copying the
        # entire dict.  write_scope and capability_profile are always
        # overwritten per-sub-agent, so they do not need deep copies.
        parent_ctx = self.registry._context
        sub_registry._context = dict(parent_ctx)
        blocked = parent_ctx.get("shell_blocked_commands")
        if isinstance(blocked, list):
            sub_registry._context["shell_blocked_commands"] = list(blocked)
        if write_scope:
            sub_registry._context["write_scope"] = list(write_scope)
        sub_registry._context["capability_profile"] = capability_profile
        return sub_registry

    def _compose_sub_system_prompt(
        self,
        sub_registry: "ToolRegistry",
        system_suffix: str = "",
    ) -> tuple[str, Optional[AgentContext]]:
        # Always build system prompt from base_system_prompt + sub_registry
        # so it reflects only the tools the sub-agent actually has, and does NOT
        # include transient per-turn LTM injections from the parent's active context.
        output_dir_str = sub_registry._context.get("output_dir")
        output_dir_path = Path(output_dir_str) if output_dir_str else None
        active_ctx = self.current_context()
        # Only pass a real SkillCatalog instance — metadata may contain test
        # stubs or other objects that lack the summary_lines() method.
        skill_catalog_for_prompt: Optional[SkillCatalog] = None
        if active_ctx:
            sc = active_ctx.metadata.get("skill_catalog")
            if isinstance(sc, SkillCatalog):
                skill_catalog_for_prompt = sc
        sys_prompt = _compose_system_prompt(
            self._base_system_prompt,
            sub_registry,
            self.workspace_root,
            output_dir=output_dir_path,
            skill_catalog=skill_catalog_for_prompt,
        )
        if system_suffix:
            sys_prompt += f"\n\n{system_suffix}"
        return sys_prompt, active_ctx

    @staticmethod
    def _propagate_sub_metadata(
        sub_ctx: AgentContext,
        active_ctx: Optional[AgentContext],
    ) -> None:
        # Propagate skill metadata so sub-agents can also activate skills.
        if active_ctx:
            if "skill_catalog" in active_ctx.metadata:
                sub_ctx.metadata["skill_catalog"] = active_ctx.metadata[
                    "skill_catalog"
                ]
            if "required_skills" in active_ctx.metadata:
                sub_ctx.metadata["required_skills"] = list(
                    active_ctx.metadata["required_skills"]
                )

    def _create_sub_agent(self, sub_registry: "ToolRegistry") -> "BaseAgent":
        sub_agent = BaseAgent(
            self.client,
            sub_registry,
            model=self.model,
            max_tokens=self.max_tokens,
            api_format=self.api_format,
            supports_vision=self.supports_vision,
        )
        sub_agent.context_manager = self.context_manager
        sub_agent.max_parallel_agents = self.max_parallel_agents
        sub_agent.sub_agent_timeout_seconds = self.sub_agent_timeout_seconds
        sub_agent.sub_agent_retries = self.sub_agent_retries
        sub_agent.max_tool_call_iterations = self.max_tool_call_iterations
        sub_agent.result_content_max_chars = self.result_content_max_chars
        return sub_agent

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
            model=self.model,
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
        limit = max(500, self.result_content_max_chars)
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
    ) -> dict[str, Any]:
        """Execute a sub-agent and return a structured result dict.

        This is the single execution primitive for all sub-agent runs —
        the spawn_agent tool and the orchestrated _execute_subtask_spec
        both route through it, eliminating the JSON round-trip that
        existed when _execute_subtask_spec called spawn_agent via
        registry.call().
        """
        normalized_scope = [str(item) for item in (write_scope or []) if str(item).strip()]
        normalized_output_contract = self._normalize_output_contract(output_contract)
        # If the role names a plugin-declared agent (``plugin:<P>:<A>``),
        # prepend its markdown body to system_suffix so the sub-agent inherits
        # the plugin author's role definition.
        if role.startswith("plugin:") and self.plugin_catalog is not None:
            defn = self.plugin_catalog.get_agent_definition(role)
            if defn is not None:
                agent_body = str(defn.get("body", "")).strip()
                if agent_body:
                    system_suffix = (
                        f"{agent_body}\n\n{system_suffix}".strip()
                        if system_suffix
                        else agent_body
                    )
        shaped_task = self._with_output_contract(
            task,
            expected_output,
            normalized_output_contract,
        )
        # sub_registry / sub_agent / sys_prompt are pure functions of the
        # input arguments — build once and reuse across retry attempts.
        # Only sub_ctx (which carries mutable conversation state) must be
        # rebuilt per attempt to avoid contamination from a failed run.
        sub_registry = self._create_sub_registry(
            capability_profile=capability_profile,
            write_scope=normalized_scope,
        )
        sub_agent = self._create_sub_agent(sub_registry)
        sys_prompt, active_ctx = self._compose_sub_system_prompt(sub_registry, system_suffix)

        def _fresh_sub_ctx() -> AgentContext:
            ctx = AgentContext(role=role, system_prompt=sys_prompt)
            self._propagate_sub_metadata(ctx, active_ctx)
            if handoff:
                ctx.system_prompt += (
                    "\n\n## Handoff data from upstream\n"
                    + json.dumps(handoff, ensure_ascii=False, indent=2)
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
        # Only auto-retry read-only profiles — implementation workers may
        # have written files or produced side effects that a retry cannot
        # roll back.  "full" profile is excluded for the same reason.
        retries = int(self.sub_agent_retries)
        if capability_profile not in {"read_only", "research"}:
            retries = 0
        max_attempts = max(0, retries) + 1
        result = None
        started_at = time.monotonic()  # overwritten per attempt; init for type-checker
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                sub_ctx = _fresh_sub_ctx()
                self._emit_subagent_event(
                    SubAgentProgressEvent(
                        kind="agent_retry",
                        role=role,
                        task=shaped_task,
                        message=f"{role} retry {attempt}/{max_attempts}",
                        metrics={
                            "capability_profile": capability_profile,
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                        },
                    )
                )
            started_at = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    sub_agent.send_message(sub_ctx, shaped_task),
                    timeout=self.sub_agent_timeout_seconds,
                )
                break  # success — exit retry loop
            except asyncio.TimeoutError:
                if attempt < max_attempts:
                    continue
                partial = ""
                for msg in reversed(sub_ctx.messages):
                    if msg.get("role") == "assistant" and msg.get("content"):
                        partial = str(msg["content"])[:500]
                        break
                payload: dict[str, Any] = {
                    "ok": False,
                    "role": role,
                    "task": shaped_task,
                    "timed_out": True,
                    "error": (
                        f"sub-agent timed out after {self.sub_agent_timeout_seconds}s"
                    ),
                }
                if partial:
                    payload["partial_content"] = partial
                elapsed = time.monotonic() - started_at
                self._emit_subagent_event(
                    SubAgentProgressEvent(
                        kind="agent_failed",
                        role=role,
                        task=shaped_task,
                        message=payload["error"],
                        metrics={
                            "capability_profile": capability_profile,
                            "write_scope_count": len(normalized_scope),
                            "duration_seconds": elapsed,
                            "attempts": attempt,
                        },
                    )
                )
                return payload
            except Exception as e:
                if attempt < max_attempts:
                    continue
                payload = {
                    "ok": False,
                    "role": role,
                    "task": shaped_task,
                    "error": f"sub-agent failed: {self._format_agent_error(e)}",
                }
                elapsed = time.monotonic() - started_at
                self._emit_subagent_event(
                    SubAgentProgressEvent(
                        kind="agent_failed",
                        role=role,
                        task=shaped_task,
                        message=payload["error"],
                        metrics={
                            "capability_profile": capability_profile,
                            "write_scope_count": len(normalized_scope),
                            "duration_seconds": elapsed,
                            "attempts": attempt,
                        },
                    )
                )
                return payload

        # If we reach here, the for-loop hit `break` after a successful attempt;
        # all failure paths return inside the loop body above.
        assert result is not None
        payload: dict[str, Any] = {
            "ok": result.error is None,
            "role": role,
            "task": shaped_task,
            "content": result.content or "(no output)",
            "tool_calls_made": result.tool_calls_made,
        }
        contract_ok, contract_content, structured_content, contract_error = (
            self._validate_output_contract(
                result.content or "",
                expected_output=expected_output,
                output_contract=normalized_output_contract,
            )
        )
        if not contract_ok:
            payload["ok"] = False
            payload["error"] = contract_error
        else:
            payload["content"] = contract_content
            if structured_content is not None:
                payload["structured_content"] = structured_content
        if result.error:
            payload["error"] = result.error
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
        # Summarize overly long results to keep the parent's context window
        # bounded.  Full content is preserved in the staging buffer for
        # later retrieval via LTM, so the parent can always look up details.
        full_content = payload["content"]
        if (
            self.result_content_max_chars > 0
            and len(full_content) > self.result_content_max_chars
        ):
            summary = await self._summarize_text(
                full_content, role, shaped_task
            )
            payload["content"] = summary
            payload["full_content"] = full_content
        # Persist sub-agent findings directly to LTM rather than the
        # staging buffer.  Staging is reserved for real user/assistant
        # conversation turns; sub-agent traces must not be mixed in or
        # consolidation will extract them as user-visible episodes.
        if self.context_manager and payload["ok"]:
            with shared._suppress_with_log("LTM write for sub-agent result failed; turn continues"):
                content_to_store = full_content
                entry_id = shared._new_id()
                now = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
                self.context_manager.store.write_entry(
                    LTMEntry(
                        id=entry_id,
                        content=(
                            f"sub-agent [{role}] task: {shaped_task[:500]}\n\n"
                            f"result: {content_to_store[:2000]}"
                        ),
                        importance=0.5,
                        category="episodes",
                        entity=role,
                        memory_type="sub_agent_observation",
                        scope="global",
                        status="active",
                        source_session=(
                            self.context_manager.staging.session_id or ""
                        ),
                        confidence=0.7,
                        created_at=now,
                        updated_at=now,
                    )
                )
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
                        "description": "Optional list of files or directories this sub-agent may modify.",
                    },
                    "capability_profile": {
                        "type": "string",
                        "description": (
                            "Optional capability profile. Use 'read_only' for analysis workers "
                            "and 'implementation' for code-changing workers."
                        ),
                    },
                    "depends_on": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional upstream subtask ids for internal orchestration.",
                    },
                    "coordination_mode": {
                        "type": "string",
                        "description": (
                            "Optional explicit coordination mode for grouped spawn calls. "
                            "Use 'rendezvous' to request bounded multi-round coordination."
                        ),
                    },
                    "early_exit": {
                        "type": "boolean",
                        "description": (
                            "When true, if this agent succeeds, cancel all other "
                            "still-running agents in the same batch. Use for tasks "
                            "where any single finding completes the goal."
                        ),
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

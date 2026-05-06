from __future__ import annotations

import asyncio
import base64
import contextvars
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import inspect
import json
import logging
from pathlib import Path
import re
import time
from typing import Any, Callable, Optional

import anthropic

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
from agent.tools.executor import RegularToolExecutor
from agent.skills.catalog import SkillCatalog
from agent.tools.runtime import ToolRegistry

DEFAULT_SYSTEM_PROMPT = agent_module.DEFAULT_SYSTEM_PROMPT
logger = logging.getLogger(__name__)

_OPENAI_MESSAGE_RESERVED_FIELDS = frozenset(
    {
        "role",
        "content",
        "tool_calls",
        "function_call",
        "name",
        "refusal",
        "audio",
        "annotations",
        "parsed",
        "model_extra",
    }
)
_SKIP_OPENAI_EXTRA = object()


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
    """List-like context stack backed by ContextVar for per-task isolation."""

    def __init__(self) -> None:
        self._stack: contextvars.ContextVar[tuple[AgentContext, ...]] = (
            contextvars.ContextVar("agent_context_stack", default=())
        )

    def append(self, ctx: AgentContext) -> None:
        self._stack.set((*self._stack.get(), ctx))

    def pop(self) -> AgentContext:
        stack = self._stack.get()
        if not stack:
            raise IndexError("pop from empty context stack")
        self._stack.set(stack[:-1])
        return stack[-1]

    def __bool__(self) -> bool:
        return bool(self._stack.get())

    def __getitem__(self, index: int) -> AgentContext:
        return self._stack.get()[index]


class BaseAgent:
    """Core agent: streams Claude, handles tool_use loop."""

    _MAX_TRUNCATION_CONTINUATIONS = 2
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
        self.context_manager: Optional[ContextManager] = None
        self.plugin_catalog: Optional["PluginCatalog"] = None
        self.max_parallel_agents = shared.DEFAULT_MAX_PARALLEL_AGENTS
        self.sub_agent_timeout_seconds = shared.DEFAULT_SUB_AGENT_TIMEOUT_SECONDS
        self.sub_agent_retries = shared.DEFAULT_SUB_AGENT_RETRIES
        self.result_content_max_chars = shared.DEFAULT_RESULT_CONTENT_MAX_CHARS
        self.llm_max_retries = shared.DEFAULT_LLM_MAX_RETRIES
        self.llm_retry_base_delay = shared.DEFAULT_LLM_RETRY_BASE_DELAY
        self._context_stack = _TaskLocalContextStack()
        self.workspace_root: Optional[Path] = None
        self._base_system_prompt: str = ""

    def _image_content_block(self, attachment: MessageAttachment) -> dict[str, Any]:
        data = base64.b64encode(attachment.local_path.read_bytes()).decode("ascii")
        mime_type = attachment.mime_type or "application/octet-stream"
        if self.api_format == "anthropic":
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": data,
                },
            }
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{data}"},
        }

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
        return self._context_stack[-1] if self._context_stack else None

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
            has_spawn_agent="spawn_agent" in self.registry.list_tools(),
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
        return SubtaskSpec(
            id=spec.id,
            role=spec.role,
            task=task,
            depends_on=list(spec.depends_on),
            expected_output=spec.expected_output,
            output_contract=dict(spec.output_contract),
            write_scope=list(spec.write_scope),
            capability_profile=spec.capability_profile,
            handoff=handoff,
            early_exit=spec.early_exit,
        )

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
        return SubtaskSpec(
            id=spec.id,
            role=spec.role,
            task=task,
            depends_on=list(spec.depends_on),
            expected_output=spec.expected_output,
            output_contract=dict(spec.output_contract),
            write_scope=list(spec.write_scope),
            capability_profile=spec.capability_profile,
            handoff=handoff,
            early_exit=spec.early_exit,
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
        try:
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
        except Exception:
            pass
        # Fallback: original string concatenation
        summary_lines = []
        for result in results:
            status = "ok" if result.ok else "error"
            detail = result.summary or result.error or ""
            summary_lines.append(
                f"- {result.id} ({status}): {detail}".rstrip()
            )
        return "\n".join(summary_lines)

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
        if not tools:
            return anthropic.NOT_GIVEN if self.api_format == "anthropic" else None
        if self.api_format == "openai":
            # Convert Anthropic tool schema → OpenAI function-calling format
            return [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {}),
                    },
                }
                for t in tools
            ]
        return tools  # anthropic format as-is

    def _inject_system(self, messages: list[dict], system_prompt: str) -> list[dict]:
        """For OpenAI format, prepend system as first message."""
        if self.api_format == "openai":
            return [{"role": "system", "content": system_prompt}] + messages
        return messages  # Anthropic passes system separately

    async def _create(self, ctx: "AgentContext", tools: list[dict]) -> Any:
        """Non-streaming API call, returns a normalised response object."""
        if self.api_format == "anthropic":
            return await self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=ctx.system_prompt,
                messages=ctx.messages,
                tools=self._tools_for_api(tools),
            )
        else:
            # OpenAI-compatible
            kwargs: dict = dict(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=self._inject_system(ctx.messages, ctx.system_prompt),
            )
            api_tools = self._tools_for_api(tools)
            if api_tools:
                kwargs["tools"] = api_tools
            return await self.client.chat.completions.create(**kwargs)

    def _parse_response(self, response: Any) -> tuple[str, str, list[dict]]:
        """
        Parse a response object into (stop_reason, text, tool_calls).
        tool_calls: list of {"name": ..., "id": ..., "input": {...}}
        """
        if self.api_format == "anthropic":
            stop_reason = response.stop_reason  # "end_turn" | "tool_use"
            text_blocks = [b for b in response.content if hasattr(b, "text")]
            text = " ".join(b.text for b in text_blocks)
            tool_calls = [
                {"name": b.name, "id": b.id, "input": b.input}
                for b in response.content
                if b.type == "tool_use"
            ]
            return stop_reason, text, tool_calls
        else:
            # OpenAI
            choice = response.choices[0]
            finish = choice.finish_reason  # "stop" | "tool_calls"
            msg = choice.message
            text = msg.content or ""
            if finish == "tool_calls" and msg.tool_calls:
                tool_calls = []
                for tc in msg.tool_calls:
                    try:
                        inp = json.loads(tc.function.arguments)
                    except Exception:
                        inp = {}
                    tool_calls.append(
                        {"name": tc.function.name, "id": tc.id, "input": inp}
                    )
                return "tool_use", text, tool_calls
            return "end_turn", text, []

    def _response_completion_error(self, response: Any) -> Optional[str]:
        """Classify provider completion states that should not be treated as clean ends."""
        if self.api_format != "openai":
            return None
        try:
            finish = response.choices[0].finish_reason
        except Exception:
            return None
        if finish == "length":
            return "Model response was truncated (finish_reason=length)"
        return None

    @staticmethod
    def _openai_message_extras(message: Any) -> dict[str, Any]:
        """Extract provider-specific fields from OpenAI-compatible messages/deltas."""
        if message is None:
            return {}

        extras: dict[str, Any] = {}

        if isinstance(message, dict):
            for key, value in message.items():
                if key in _OPENAI_MESSAGE_RESERVED_FIELDS:
                    continue
                sanitized = BaseAgent._sanitize_openai_extra_value(value)
                if sanitized is not _SKIP_OPENAI_EXTRA:
                    extras[key] = sanitized
            model_extra = message.get("model_extra")
            if isinstance(model_extra, dict):
                for key, value in model_extra.items():
                    if key in _OPENAI_MESSAGE_RESERVED_FIELDS:
                        continue
                    sanitized = BaseAgent._sanitize_openai_extra_value(value)
                    if sanitized is not _SKIP_OPENAI_EXTRA:
                        extras[key] = sanitized
            return extras

        raw_fields = getattr(message, "__dict__", None)
        if isinstance(raw_fields, dict):
            for key, value in raw_fields.items():
                if key.startswith("_") or key in _OPENAI_MESSAGE_RESERVED_FIELDS:
                    continue
                sanitized = BaseAgent._sanitize_openai_extra_value(value)
                if sanitized is not _SKIP_OPENAI_EXTRA:
                    extras[key] = sanitized

        model_extra = getattr(message, "model_extra", None)
        if isinstance(model_extra, dict):
            for key, value in model_extra.items():
                if key in _OPENAI_MESSAGE_RESERVED_FIELDS:
                    continue
                sanitized = BaseAgent._sanitize_openai_extra_value(value)
                if sanitized is not _SKIP_OPENAI_EXTRA:
                    extras[key] = sanitized
        return extras

    @classmethod
    def _sanitize_openai_extra_value(cls, value: Any) -> Any:
        """Keep only JSON-safe provider extras that can be sent back to the API."""
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            cleaned: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    continue
                sanitized = cls._sanitize_openai_extra_value(item)
                if sanitized is _SKIP_OPENAI_EXTRA:
                    continue
                cleaned[key] = sanitized
            return cleaned
        if isinstance(value, (list, tuple)):
            cleaned_list: list[Any] = []
            for item in value:
                sanitized = cls._sanitize_openai_extra_value(item)
                if sanitized is _SKIP_OPENAI_EXTRA:
                    continue
                cleaned_list.append(sanitized)
            return cleaned_list
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return cls._sanitize_openai_extra_value(model_dump(mode="python"))
            except Exception:
                return _SKIP_OPENAI_EXTRA
        return _SKIP_OPENAI_EXTRA

    @classmethod
    def _merge_openai_extra_value(cls, current: Any, incoming: Any) -> Any:
        """Merge streamed provider extras while preserving chunked text fields."""
        incoming = cls._sanitize_openai_extra_value(incoming)
        if incoming is _SKIP_OPENAI_EXTRA:
            return copy.deepcopy(current)
        current = cls._sanitize_openai_extra_value(current)
        if current is _SKIP_OPENAI_EXTRA:
            current = None
        if current is None:
            return copy.deepcopy(incoming)
        if incoming is None:
            return copy.deepcopy(current)
        if isinstance(current, str) and isinstance(incoming, str):
            if incoming == current:
                return current
            if incoming.startswith(current):
                return incoming
            if current.startswith(incoming) or current.endswith(incoming):
                return current
            return current + incoming
        if isinstance(current, dict) and isinstance(incoming, dict):
            merged = copy.deepcopy(current)
            for key, value in incoming.items():
                merged[key] = cls._merge_openai_extra_value(merged.get(key), value)
            return merged
        return copy.deepcopy(incoming)

    @classmethod
    def _merge_openai_message_extras(
        cls, current: dict[str, Any], incoming: dict[str, Any]
    ) -> dict[str, Any]:
        merged = copy.deepcopy(current)
        for key, value in incoming.items():
            merged[key] = cls._merge_openai_extra_value(merged.get(key), value)
        return merged

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
        if self.api_format == "anthropic":
            return {"role": "assistant", "content": response.content}
        else:
            # For OpenAI we store the raw message object (or a dict)
            msg = response.choices[0].message
            entry: dict = {"role": "assistant", "content": text}
            entry.update(self._openai_message_extras(msg))
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            return entry

    def _tool_result_messages(
        self, tool_calls: list[dict], results: list[str]
    ) -> list[dict]:
        """Build tool-result history entries for both formats."""
        if self.api_format == "anthropic":
            return [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": tc["id"], "content": r}
                        for tc, r in zip(tool_calls, results)
                    ],
                }
            ]
        else:
            # OpenAI: one message per tool result
            return [
                {"role": "tool", "tool_call_id": tc["id"], "content": r}
                for tc, r in zip(tool_calls, results)
            ]

    def _format_agent_error(self, exc: Exception) -> str:
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return "Model request timed out"
        if isinstance(exc, ValueError):
            return f"Invalid model request: {exc}"
        return str(exc) or exc.__class__.__name__

    @staticmethod
    def _is_llm_retryable(exc: Exception) -> bool:
        """Return True for transient errors worth retrying."""
        # Network / timeout errors
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
            return True
        cls_name = exc.__class__.__name__
        error_msg = str(exc).lower()
        # Anthropic SDK error types
        if cls_name in (
            "RateLimitError", "APIConnectionError", "APITimeoutError",
            "InternalServerError", "APIStatusError",
        ):
            return True
        # OpenAI SDK error types (may be imported lazily)
        if cls_name in (
            "RateLimitError", "APIConnectionError", "APITimeoutError",
            "InternalServerError",
        ):
            return True
        # Fallback: check error message for known transient patterns
        retryable_keywords = (
            "rate limit", "too many requests", "429",
            "server error", "500", "502", "503", "504",
            "timeout", "timed out", "connection", "network",
            "service unavailable", "overloaded",
        )
        if any(kw in error_msg for kw in retryable_keywords):
            return True
        return False

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
    def _synthesize_tool_only_response(
        tool_history: list[tuple[str, str]]
    ) -> str:
        for tool_name, raw_result in reversed(tool_history):
            if tool_name != "schedule_create":
                continue
            try:
                payload = json.loads(raw_result)
            except Exception:
                continue
            if not isinstance(payload, dict) or not payload.get("ok"):
                continue
            summary_text = str(payload.get("summary_text", "")).strip()
            if summary_text:
                return summary_text
        return ""

    async def _run_tool_uses(
        self,
        tool_uses: list[dict],
        orchestration_decision: OrchestrationDecision | None = None,
    ) -> list[str]:
        # M2: use a sentinel so we can distinguish "tool not run" from "tool returned empty"
        _MISSING = object()
        results: list[Any] = [_MISSING] * len(tool_uses)

        regular_calls = [
            (idx, tu) for idx, tu in enumerate(tool_uses) if tu["name"] != "spawn_agent"
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
            (idx, tu) for idx, tu in enumerate(tool_uses) if tu["name"] == "spawn_agent"
        ]
        if spawn_calls:
            # All spawn calls go through a single dispatch path via
            # _run_orchestrated_spawn_calls, which routes to the
            # appropriate runtime (parallel, pipeline, rendezvous) or
            # executes directly for a single unorchestrated spawn.
            # The separate inline semaphore+heartbeat path that used to
            # live here has been removed — its functionality is now
            # handled by the runtime functions.
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
            return [
                r
                if r is not _MISSING
                else json.dumps({"ok": False, "error": "tool result missing"})
                for r in results
            ]

        # M2: replace any slot that was never assigned (programming error guard)
        return [
            r
            if r is not _MISSING
            else json.dumps({"ok": False, "error": "tool result missing"})
            for r in results
        ]

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

        # B1: wrap ALL mutations (prompt injection, messages append, stack push)
        # inside the try/finally so they are always cleaned up on error.
        try:
            # Inject relevant context into system prompt for this turn.
            # retrieve_context() includes both:
            #   1. Recent staging buffer turns (current session, not yet consolidated)
            #   2. LTM search results (historical sessions)
            # Using retrieve_ltm_context() alone would miss any conversation from
            # the current session that has been compacted out of ctx.messages but
            # not yet consolidated into LTM, causing the agent to "forget" recent
            # turns when asked about them.
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
            orchestration_decision = self._plan_orchestration(ctx, user_message)

            ctx.messages.append(
                {
                    "role": "user",
                    "content": self._build_user_message_content(user_message, attachments),
                }
            )
            self._context_stack.append(ctx)

            # D1: bounded tool-call loop — prevents infinite model loops
            for _iteration in range(shared.MAX_TOOL_CALL_ITERATIONS + 1):
                trace_iterations = _iteration + 1
                if _iteration == shared.MAX_TOOL_CALL_ITERATIONS:
                    trace_status = "tool_loop_exceeded"
                    trace_error = (
                        f"Tool-call loop exceeded {shared.MAX_TOOL_CALL_ITERATIONS} "
                        "iterations; possible model loop detected."
                    )
                    return AgentResult(
                        agent_id=ctx.agent_id,
                        content=result_text,
                        tool_calls_made=tool_calls_made,
                        error=trace_error,
                    )
                tools = self.registry.to_anthropic_format() if ctx.tools_enabled else []

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
                    if stream_callback:
                        response, streamed_text = await self._with_llm_retry(
                            self._stream_response, ctx, tools, stream_callback
                        )
                    else:
                        response = await self._with_llm_retry(
                            self._create, ctx, tools
                        )
                        streamed_text = ""
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
                        # M4: only update result_text from the parsed text field;
                        # do not allow streamed_text from a prior iteration to bleed in.
                        if text:
                            result_text = text
                        ctx.messages.append(self._assistant_message(response, text))

                        tool_calls_made.extend(tu["name"] for tu in tool_uses)
                        tool_use_started_at = time.perf_counter()
                        results = await self._run_tool_uses(
                            tool_uses,
                            orchestration_decision=orchestration_decision,
                        )
                        _trace_latency(
                            "tool_uses_finished",
                            agent_id=ctx.agent_id,
                            iteration=_iteration + 1,
                            total_tool_uses=len(tool_uses),
                            spawn_calls=sum(
                                1 for tool_use in tool_uses if tool_use["name"] == "spawn_agent"
                            ),
                            regular_calls=sum(
                                1 for tool_use in tool_uses if tool_use["name"] != "spawn_agent"
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
                            (tu["name"], res) for tu, res in zip(tool_uses, results)
                        )
                        ctx.messages.extend(
                            self._tool_result_messages(tool_uses, results)
                        )
                        continue
                    else:
                        # Prefer the parsed text; fall back to streamed text for
                        # the final turn (streaming accumulates what the user saw).
                        result_text = text or streamed_text or result_text
                        if not result_text and tool_result_history:
                            result_text = self._synthesize_tool_only_response(
                                tool_result_history
                            )
                        if self.api_format == "openai":
                            ctx.messages.append(
                                self._assistant_message(response, result_text)
                            )
                        else:
                            ctx.messages.append(
                                {"role": "assistant", "content": result_text}
                            )
                        completion_error = self._response_completion_error(response)
                        if completion_error:
                            result_text, continuation_error = (
                                await self._continue_truncated_response(ctx, result_text)
                            )
                            if self.api_format == "openai":
                                ctx.messages[-1] = self._assistant_message(
                                    response, result_text
                                )
                            else:
                                ctx.messages[-1] = {
                                    "role": "assistant",
                                    "content": result_text,
                                }
                            trace_status = "continuation_error"
                            trace_error = continuation_error
                            return AgentResult(
                                agent_id=ctx.agent_id,
                                content=result_text,
                                tool_calls_made=tool_calls_made,
                                error=continuation_error,
                            )
                        # Guard against silent empty responses: when the model
                        # returns stop with no text and no tool calls (often a
                        # safety-filtered or malformed response), produce a
                        # visible fallback so the user is not left with silence.
                        if not result_text and not tool_result_history:
                            result_text = (
                                "I received your message but was unable to "
                                "generate a response. Please try rephrasing "
                                "your request or checking if it triggered a "
                                "content policy filter."
                            )
                        break

                except Exception as e:
                    trace_status = "exception"
                    trace_error = self._format_agent_error(e)
                    return AgentResult(
                        agent_id=ctx.agent_id,
                        content="",
                        tool_calls_made=tool_calls_made,
                        error=trace_error,
                    )
        finally:
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
            # Always restore the original system prompt and pop the context stack.
            ctx.system_prompt = original_system
            if self._context_stack and self._context_stack[-1] is ctx:
                self._context_stack.pop()

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
        """Stream response text chunk-by-chunk and return (full_response, collected_text).

        ``callback`` may be a plain sync function or an async coroutine function;
        both are handled transparently.

        For Anthropic: uses stream.get_final_message() to obtain the complete response.
        For OpenAI: accumulates tool_call deltas and rebuilds a synthetic response.
        """
        collected: list[str] = []
        if self.api_format == "anthropic":
            async with self.client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                system=ctx.system_prompt,
                messages=ctx.messages,
                tools=self._tools_for_api(tools),
            ) as stream:
                async for text in stream.text_stream:
                    collected.append(text)
                    _r = callback(text)
                    if inspect.isawaitable(_r):
                        await _r
                response = await stream.get_final_message()
            return response, "".join(collected)

        # OpenAI streaming — accumulate tool_call deltas as well
        kwargs: dict = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=self._inject_system(ctx.messages, ctx.system_prompt),
            stream=True,
        )
        api_tools = self._tools_for_api(tools)
        if api_tools:
            kwargs["tools"] = api_tools
        finish_reason = "stop"
        tool_calls_acc: dict[int, dict] = {}  # index -> {id, name, arguments}
        provider_extras_acc: dict[str, Any] = {}
        # AsyncOpenAI.chat.completions.create() is a coroutine; await it to get
        # the AsyncStream object, then iterate the stream chunk by chunk.
        # Do NOT remove the `await` — create() returns a coroutine, not an
        # async iterable, so `async for chunk in create(...)` raises TypeError.
        async for chunk in await self.client.chat.completions.create(**kwargs):
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta.content:
                collected.append(delta.content)
                _r = callback(delta.content)
                if inspect.isawaitable(_r):
                    await _r
            delta_extras = self._openai_message_extras(delta)
            if delta_extras:
                provider_extras_acc = self._merge_openai_message_extras(
                    provider_extras_acc, delta_extras
                )
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id": tc_delta.id or "",
                            "name": (
                                tc_delta.function.name if tc_delta.function else ""
                            )
                            or "",
                            "arguments": "",
                        }
                    acc = tool_calls_acc[idx]
                    if tc_delta.id:
                        acc["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            acc["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            acc["arguments"] += tc_delta.function.arguments
            if choice.finish_reason:
                finish_reason = choice.finish_reason

        # Build a synthetic response object using module-level dataclasses
        oi_tool_calls = (
            [
                shared._OAITC(v["id"], shared._OAIFunc(v["name"], v["arguments"]))
                for _, v in sorted(tool_calls_acc.items())
            ]
            if tool_calls_acc
            else None
        )

        response = shared._OAIResponse(
            [
                shared._OAIChoice(
                    finish_reason,
                    shared._OAIMsg(
                        "".join(collected),
                        oi_tool_calls,
                        provider_extras_acc or None,
                    ),
                )
            ]
        )
        return response, "".join(collected)

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
            if name == "spawn_agent":
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
        _summarize_text and _summarize_rendezvous_round to avoid
        duplicated Anthropic / OpenAI dispatch logic.
        """
        try:
            if self.api_format == "anthropic":
                resp = await self.client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                )
                if resp.content and hasattr(resp.content[0], "text"):
                    return resp.content[0].text.strip()
            else:
                resp = await self.client.chat.completions.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                )
                if resp.choices and resp.choices[0].message.content:
                    return resp.choices[0].message.content.strip()
        except Exception:
            pass
        return None

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
        shaped_task = self._with_output_contract(
            task,
            expected_output,
            normalized_output_contract,
        )
        sub_registry = self._create_sub_registry(
            capability_profile=capability_profile,
            write_scope=normalized_scope,
        )
        sub_agent = self._create_sub_agent(sub_registry)
        sys_prompt, active_ctx = self._compose_sub_system_prompt(sub_registry, system_suffix)
        sub_ctx = AgentContext(role=role, system_prompt=sys_prompt)
        self._propagate_sub_metadata(sub_ctx, active_ctx)
        if handoff:
            sub_ctx.system_prompt += (
                "\n\n## Handoff data from upstream\n"
                + json.dumps(handoff, ensure_ascii=False, indent=2)
            )
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
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                # Fresh sub-agent and context for each retry to avoid state
                # contamination from the failed attempt.
                sub_registry = self._create_sub_registry(
                    capability_profile=capability_profile,
                    write_scope=normalized_scope,
                )
                sub_agent = self._create_sub_agent(sub_registry)
                sys_prompt, active_ctx = self._compose_sub_system_prompt(
                    sub_registry, system_suffix
                )
                sub_ctx = AgentContext(role=role, system_prompt=sys_prompt)
                self._propagate_sub_metadata(sub_ctx, active_ctx)
                if handoff:
                    sub_ctx.system_prompt += (
                        "\n\n## Handoff data from upstream\n"
                        + json.dumps(handoff, ensure_ascii=False, indent=2)
                    )
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
            try:
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
            except Exception:
                pass  # storage failure must not break the parent turn
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

"""The facades BaseAgent hands to the turn loop, the call leg and the spawner: a sub-registry, the retry policy, the effective model, the write-scope canonicalizer, and the small adapters over the spawn planner. Moved out of BaseAgent (see the remaining-big-files survey work order). Each one is handed exactly the state it reads."""

from __future__ import annotations

from agent import shared

from agent.core.output_contract import output_contract_path_resolver

from agent.core.subagent_context import canonicalize_write_scope, compose_sub_system_prompt

from agent.config import _compose_system_prompt

from agent.tools.runtime import ToolView

from agent.skills.catalog import SkillCatalog

from agent.core.retry import RetryPolicy

from pathlib import Path

from agent.orchestration.planner import OrchestrationDecision

from agent.memory.system import ContextManager

from agent.orchestration.runtime import CAPABILITY_PROFILES, SubtaskResult, SubtaskSpec

from typing import Any, Optional

from agent.core.turn_state import AgentContext

__all__ = [
    "_create_sub_registry",
    "_call_llm",
    "_compose_sub_system_prompt",
    "_retry_policy",
    "_effective_model",
    "_spawn_tool_use_to_spec",
    "_default_capability_profile",
    "_spawn_result_payload",
    "_result_content_limit",
    "_canonicalize_write_scope",
    "_output_contract_path_resolver",
    "_derive_execution_mode_from_spawn_calls",
    "_max_agents_per_turn",
    "_context_manager_for",
]


def _context_manager_for(
    ctx: Optional["AgentContext"] = None,
    *,
    context_manager,
    current_context,
) -> Optional[ContextManager]:
    request_ctx = ctx or current_context()
    if request_ctx is not None:
        scoped = request_ctx.metadata.get("context_manager")
        if scoped is not None:
            return scoped
    return context_manager


def _effective_model(
    ctx: Optional["AgentContext"] = None,
    *,
    model,
    current_context,
) -> str:
    request_ctx = ctx or current_context()
    override = (
        request_ctx.metadata.get("model_override") if request_ctx else None
    )
    if override is None:
        return model
    if not isinstance(override, str):
        raise TypeError("model override must be a nonblank string or None")
    if not override.strip():
        raise ValueError("model override must not be blank")
    return override


def _result_content_limit(
    *,
    result_content_max_chars,
) -> int:
    return min(
        shared.MAX_RESULT_CONTENT_CHARS,
        max(
            shared.MIN_RESULT_CONTENT_CHARS,
            int(result_content_max_chars or 0),
        ),
    )


def _output_contract_path_resolver(
    *,
    registry,
    workspace_root,
):
    """A resolver bound to this agent's output dir and workspace root."""
    output_dir_str = registry.get_context("output_dir")
    return output_contract_path_resolver(
        output_dir=Path(output_dir_str) if output_dir_str else None,
        workspace_root=workspace_root,
    )


def _default_capability_profile(
    role: str,
    task: str,
    explicit: str,
    *,
    orchestration_mode: str,
    spawn_planner,
) -> str:
    return spawn_planner.default_capability_profile(
        role, task, explicit, orchestration_mode=orchestration_mode
    )


def _spawn_tool_use_to_spec(
    tool_use: dict[str, Any],
    *,
    index: int,
    orchestration_decision: OrchestrationDecision,
    spawn_planner,
) -> SubtaskSpec:
    """See :meth:`agent.core.spawn_planning.SpawnPlanner.spawn_tool_use_to_spec`."""
    return spawn_planner.spawn_tool_use_to_spec(
        tool_use, index=index, orchestration_decision=orchestration_decision
    )


def _max_agents_per_turn(
    *,
    spawn_planner,
) -> int:
    """Total sub-agents one turn may run, independent of concurrency."""
    return spawn_planner.max_agents_per_turn()


def _spawn_result_payload(
    spec: SubtaskSpec,
    result: SubtaskResult,
    *,
    extra: dict[str, Any] | None = None,
    spawn_planner,
) -> str:
    """See :meth:`agent.core.spawn_planning.SpawnPlanner.spawn_result_payload`."""
    return spawn_planner.spawn_result_payload(spec, result, extra=extra)


def _derive_execution_mode_from_spawn_calls(
    spawn_calls: list[tuple[int, dict]],
    *,
    spawn_planner,
) -> str:
    """See :meth:`agent.core.spawn_planning.SpawnPlanner.execution_mode_from_spawn_calls`."""
    return spawn_planner.execution_mode_from_spawn_calls(spawn_calls)


def _canonicalize_write_scope(
    raw_scope: str,
    *,
    registry,
    workspace_root,
) -> Path:
    """See :func:`agent.core.subagent_context.canonicalize_write_scope`."""
    output_dir_str = registry.get_context("output_dir")
    return canonicalize_write_scope(
        raw_scope,
        workspace_root=workspace_root,
        output_dir=Path(output_dir_str) if output_dir_str else None,
    )


def _retry_policy(
    *,
    llm_event_bus,
    llm_max_retries,
    llm_retry_base_delay,
    is_llm_retryable,
) -> RetryPolicy:
    """This agent's retry policy, in the shape the retry leg takes.

        The policy is an object rather than three arguments because the retry leg is
        a module function now: ``agent/core/llm_attempt.py`` calls it and documents
        that it does not import ``agent.core.agent``, so handing it an agent would
        have undone that.  Three values keep the promise.
        """
    return RetryPolicy(
        max_retries=llm_max_retries,
        base_delay=llm_retry_base_delay,
        event_bus=llm_event_bus,
        is_retryable=is_llm_retryable,
    )


def _create_sub_registry(
    capability_profile: str = "full",
    write_scope: list[str] | None = None,
    *,
    registry,
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
    return registry.derive(
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
    sub_registry: "ToolRegistry",
    system_suffix: str = "",
    *,
    workspace_root,
    base_system_prompt,
    current_context,
) -> tuple[str, Optional[AgentContext]]:
    """See :func:`agent.core.subagent_context.compose_sub_system_prompt`."""
    return compose_sub_system_prompt(
        sub_registry,
        base_system_prompt=base_system_prompt,
        workspace_root=workspace_root,
        active_context=current_context(),
        system_suffix=system_suffix,
        compose=_compose_system_prompt,
        skill_catalog_type=SkillCatalog,
    )


async def _call_llm(
    prompt: str,
    *,
    system: str,
    max_tokens: int = 1024,
    transport,
    model,
    current_context,
) -> str | None:
    """Make a lightweight, tool-free LLM call.

        Returns the response text, or None if the call fails.  Used by
        _summarize_text and _summarize_rendezvous_round.  Dispatch lives
        in the transport — provider-specific dispatch is not the agent's job.
        """
    return await transport.simple_chat(
        model=_effective_model(model=model, current_context=current_context),
        max_tokens=max_tokens,
        system=system,
        prompt=prompt,
    )

"""Preparing a sub-agent before any model call.

Why this module exists
----------------------
``_execute_agent`` opens with 48 lines that decide what the sub-agent *is*:
which output contract it will be held to, which tools its registry exposes,
which plugin-declared role definition is prepended, what its system prompt says,
and what metadata its context inherits.  None of that talks to a model, so none
of it needed an agent to test -- it needed the eight things it reads, passed in.

After this the rest of ``_execute_agent`` is: run the loop, then finalize.  Each
of those is a separate extraction; this one is first because it produces the
values the other two consume.

The three normalizations
------------------------
Three inputs arrive in whatever shape a tool call happened to use and have to be
made canonical before anything reads them:

- **the write scope** -- a list of strings, blanks dropped, so the capability
  profile and the registry view agree on what the sub-agent may touch;
- **the output contract** -- parsed into the canonical dict form, because the
  prompt builder and the validator must read the same shape or one of them
  silently sees an empty contract;
- **the task** -- already carrying its contract instructions, since the model
  cannot satisfy a contract it was never told about.

A plugin-declared role
----------------------
When ``role`` names a plugin's agent definition (``plugin:<P>:<A>``), the
author's markdown body is prepended to the system suffix.  It is prepended
rather than replacing the caller's suffix so a per-spawn instruction still wins
over the standing role text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

__all__ = ["SubAgentPreparation", "prepare_sub_agent"]


@dataclass(frozen=True)
class SubAgentPreparation:
    """Everything the loop and the finalizer need, resolved once.

    Deliberately built before the retry loop: the registry, the sub-agent and
    the system prompt are pure functions of the arguments, so rebuilding them per
    attempt would be wasted work and would give two attempts different tool
    schemas.  Only the conversation context is rebuilt per attempt, because that
    is the state a retry must not inherit.
    """

    normalized_scope: tuple[str, ...]
    output_contract: dict[str, Any]
    shaped_task: str
    sub_registry: Any
    sub_agent: Any
    system_prompt: str
    active_context: Any


def prepare_sub_agent(
    *,
    role: str,
    task: str,
    system_suffix: str = "",
    expected_output: str = "",
    output_contract: Optional[dict[str, Any]] = None,
    write_scope: Optional[list[str]] = None,
    capability_profile: str = "full",
    plugin_catalog: Any = None,
    normalize_output_contract: Callable[[Any], dict[str, Any]],
    with_output_contract: Callable[..., str],
    create_sub_registry: Callable[..., Any],
    create_sub_agent: Callable[[Any], Any],
    compose_system_prompt: Callable[..., Any],
) -> SubAgentPreparation:
    """Resolve everything about a sub-agent that does not depend on a model call.

    The five callables are the agent's own, injected rather than imported: this
    module must not depend on ``agent.core.agent``, which is where it is used
    from.
    """
    normalized_scope = [
        str(item) for item in (write_scope or []) if str(item).strip()
    ]
    normalized_output_contract = normalize_output_contract(output_contract)

    # A plugin-declared role contributes its author's markdown body.  Prepended,
    # so a per-spawn suffix still comes last and therefore wins.
    if role.startswith("plugin:") and plugin_catalog is not None:
        definition = plugin_catalog.get_agent_definition(role)
        if definition is not None:
            body = str(definition.get("body", "")).strip()
            if body:
                system_suffix = (
                    f"{body}\n\n{system_suffix}".strip()
                    if system_suffix
                    else body
                )

    shaped_task = with_output_contract(task, expected_output, normalized_output_contract)
    sub_registry = create_sub_registry(
        capability_profile=capability_profile,
        write_scope=normalized_scope,
    )
    sub_agent = create_sub_agent(sub_registry)
    system_prompt, active_context = compose_system_prompt(sub_registry, system_suffix)

    return SubAgentPreparation(
        normalized_scope=tuple(normalized_scope),
        output_contract=normalized_output_contract,
        shaped_task=shaped_task,
        sub_registry=sub_registry,
        sub_agent=sub_agent,
        system_prompt=system_prompt,
        active_context=active_context,
    )

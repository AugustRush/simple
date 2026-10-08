"""Building a sub-agent from its parent's configuration.

Why this module exists
----------------------
``_create_sub_agent`` was 27 lines reaching fifteen attributes of its parent.
That is one thing, not fifteen: a sub-agent is the parent's configuration plus a
narrower tool registry.  Writing those fifteen down as a frozen snapshot makes
the inheritance an auditable contract instead of twelve assignment lines buried
in a 4k-line class -- and it is the precondition for extracting
``_execute_agent``, because after this the parent supplies data rather than
being reached into.

The transport is inherited, not rebuilt
---------------------------------------
The one line worth keeping a comment on: the sub-agent receives the parent's
``transport`` rather than building one from ``client``.  The transport carries
the model → provider routing table, and a sub-agent runs on the very model the
turn resolved, so rebuilding it would pin the sub-agent to the *active* provider.
A per-turn override naming another provider's model -- which the composer
dropdown offers -- would then be posted to the active provider's endpoint and
rejected with a 400.

What is *not* inherited
-----------------------
``context_manager`` is passed separately because it is resolved per request
rather than read off the parent, and the tool registry is the caller's, since the
whole point of a sub-agent is a narrower one.  Everything else is configuration
and travels verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

__all__ = ["AgentSettings", "SubAgentSpec", "build_sub_agent", "settings_from"]


@dataclass(frozen=True)
class AgentSettings:
    """The parent configuration a sub-agent inherits.

    A snapshot, not a live view: a sub-agent runs with the limits the turn had
    when it was spawned, so a later config edit cannot change a running child's
    ceiling mid-flight.
    """

    model: str
    max_tokens: int
    api_format: str
    supports_vision: bool
    context_window: int
    output_reserve: int
    max_parallel_agents: int
    sub_agent_timeout_seconds: float
    sub_agent_retries: int
    max_tool_call_iterations: int
    max_truncation_continuations: int
    result_content_max_chars: int


@dataclass(frozen=True)
class SubAgentSpec:
    """What the builder is handed: the parent's material, and the child's view."""

    client: Any
    transport: Any
    registry: Any
    settings: AgentSettings
    context_manager: Any = None
    #: The parent's event buses, inherited verbatim -- see :func:`build_sub_agent`.
    event_bus: Any = None
    llm_event_bus: Any = None
    tool_event_bus: Any = None


def settings_from(agent: Any) -> AgentSettings:
    """Snapshot an agent's inherited configuration.

    ``getattr`` with defaults for the two fields a test stub may not carry,
    because a stub that exercises the builder should not have to reproduce the
    whole constructor.
    """
    return AgentSettings(
        model=agent._effective_model(),
        max_tokens=int(agent.max_tokens),
        api_format=str(agent.api_format),
        supports_vision=bool(agent.supports_vision),
        context_window=int(agent.context_window),
        output_reserve=int(agent.output_reserve),
        max_parallel_agents=int(agent.max_parallel_agents),
        sub_agent_timeout_seconds=float(agent.sub_agent_timeout_seconds),
        sub_agent_retries=int(agent.sub_agent_retries),
        max_tool_call_iterations=int(agent.max_tool_call_iterations),
        max_truncation_continuations=int(agent.max_truncation_continuations),
        result_content_max_chars=int(agent.result_content_max_chars),
    )


def build_sub_agent(
    spec: SubAgentSpec,
    *,
    agent_type: Callable[..., Any],
    on_built: Optional[Callable[[Any, AgentSettings], None]] = None,
) -> Any:
    """Construct one sub-agent.

    ``agent_type`` is injected rather than imported so this module does not
    depend on ``agent.core.agent`` -- which is where it is used from, and which
    would be a cycle.  ``on_built`` is an observation seam for a test that wants
    to see the agent and the settings it was given, without reproducing the
    constructor.
    """
    config = spec.settings
    # The buses are the parent's, not the process default.  A sub-agent runs *inside* the
    # parent's turn, so its turn events belong to the same composition the parent belongs
    # to: dropped here, they went to the module-level buses, where the composition's
    # subscribers could not hear them and another runtime's could.  Inherited rather than
    # rebuilt for the same reason the transport is -- anything else would be a second
    # answer to "which composition is this".
    sub_agent = agent_type(
        spec.client,
        spec.registry,
        model=config.model,
        max_tokens=config.max_tokens,
        api_format=config.api_format,
        supports_vision=config.supports_vision,
        context_window=config.context_window,
        output_reserve=config.output_reserve,
        transport=spec.transport,
        event_bus=spec.event_bus,
        llm_event_bus=spec.llm_event_bus,
        tool_event_bus=spec.tool_event_bus,
    )
    sub_agent.context_manager = spec.context_manager
    sub_agent.max_parallel_agents = config.max_parallel_agents
    sub_agent.sub_agent_timeout_seconds = config.sub_agent_timeout_seconds
    sub_agent.sub_agent_retries = config.sub_agent_retries
    sub_agent.max_tool_call_iterations = config.max_tool_call_iterations
    sub_agent.max_truncation_continuations = config.max_truncation_continuations
    sub_agent.result_content_max_chars = config.result_content_max_chars
    if on_built is not None:
        on_built(sub_agent, config)
    return sub_agent

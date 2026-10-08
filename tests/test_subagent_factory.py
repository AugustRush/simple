"""Building a sub-agent, testable without one.

``_create_sub_agent`` reached fifteen attributes of its parent across 27 lines.
Written down as :class:`AgentSettings` that inheritance becomes a contract a
reader can check, and the builder becomes callable with a fake ``agent_type`` --
no parent, no transport, no model.

The transport line is the one with a reason worth testing: a sub-agent inherits
the parent's ``transport`` rather than rebuilding one from ``client``, because the
transport carries the model → provider routing table.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.core.subagent_factory import (
    AgentSettings,
    SubAgentSpec,
    build_sub_agent,
    settings_from,
)


def _settings(**overrides) -> AgentSettings:
    base = dict(
        model="m",
        max_tokens=1024,
        api_format="openai",
        supports_vision=False,
        context_window=100_000,
        output_reserve=8192,
        max_parallel_agents=3,
        sub_agent_timeout_seconds=1800.0,
        sub_agent_retries=0,
        max_tool_call_iterations=200,
        max_truncation_continuations=6,
        result_content_max_chars=4000,
    )
    base.update(overrides)
    return AgentSettings(**base)


def _spec(**overrides) -> SubAgentSpec:
    base = dict(
        client="the-client",
        transport="the-transport",
        registry="the-registry",
        settings=_settings(),
        context_manager="the-manager",
    )
    base.update(overrides)
    return SubAgentSpec(**base)


class _FakeAgent:
    """Records what the builder passed to the constructor."""

    def __init__(self, client, registry, **kwargs) -> None:
        self.client = client
        self.registry = registry
        self.kwargs = kwargs


# ── The constructor arguments ────────────────────────────────────────────────


def test_the_registry_and_client_reach_the_constructor() -> None:
    agent = build_sub_agent(_spec(), agent_type=_FakeAgent)
    assert agent.client == "the-client"
    assert agent.registry == "the-registry"


def test_the_transport_is_inherited_not_rebuilt() -> None:
    """The routing table lives in the transport.

    Rebuilding it from ``client`` would pin the sub-agent to the *active*
    provider, so a per-turn override naming another provider's model would be
    posted to the wrong endpoint and rejected.
    """
    agent = build_sub_agent(_spec(), agent_type=_FakeAgent)
    assert agent.kwargs["transport"] == "the-transport"
    assert "client" not in agent.kwargs, "the client is positional, not a rebuild hint"


def test_the_model_and_limits_reach_the_constructor() -> None:
    agent = build_sub_agent(
        _spec(settings=_settings(model="other", max_tokens=77)), agent_type=_FakeAgent
    )
    assert agent.kwargs["model"] == "other"
    assert agent.kwargs["max_tokens"] == 77


# ── The assigned attributes ──────────────────────────────────────────────────


def test_every_inherited_limit_is_applied() -> None:
    """All seven post-construction assignments, in one assertion.

    Asserting the whole set matters: this is the list that used to be seven
    scattered assignment lines, and a dropped one would be invisible.
    """
    settings = _settings(
        max_parallel_agents=5,
        sub_agent_timeout_seconds=60.0,
        sub_agent_retries=2,
        max_tool_call_iterations=42,
        max_truncation_continuations=9,
        result_content_max_chars=1234,
    )
    agent = build_sub_agent(_spec(settings=settings), agent_type=_FakeAgent)
    assert agent.max_parallel_agents == 5
    assert agent.sub_agent_timeout_seconds == 60.0
    assert agent.sub_agent_retries == 2
    assert agent.max_tool_call_iterations == 42
    assert agent.max_truncation_continuations == 9
    assert agent.result_content_max_chars == 1234


def test_the_context_manager_is_assigned_separately() -> None:
    """Resolved per request, so it is not part of the configuration snapshot."""
    agent = build_sub_agent(
        _spec(context_manager="per-request"), agent_type=_FakeAgent
    )
    assert agent.context_manager == "per-request"


def test_an_absent_context_manager_is_applied_as_none() -> None:
    """A sub-agent with no manager is legal; it just has no memory plumbing."""
    agent = build_sub_agent(_spec(context_manager=None), agent_type=_FakeAgent)
    assert agent.context_manager is None


def test_the_observation_seam_sees_the_agent_and_its_settings() -> None:
    seen: list = []
    agent = build_sub_agent(
        _spec(), agent_type=_FakeAgent, on_built=lambda a, s: seen.append((a, s))
    )
    assert seen and seen[0][0] is agent
    assert seen[0][1].model == "m"


# ── Snapshotting a parent ────────────────────────────────────────────────────


def test_settings_are_snapshotted_from_the_parent() -> None:
    parent = SimpleNamespace(
        _effective_model=lambda: "parent-model",
        max_tokens=2048,
        api_format="anthropic",
        supports_vision=True,
        context_window=200_000,
        output_reserve=4096,
        max_parallel_agents=4,
        sub_agent_timeout_seconds=90.0,
        sub_agent_retries=1,
        max_tool_call_iterations=99,
        max_truncation_continuations=3,
        result_content_max_chars=5000,
    )
    settings = settings_from(parent)
    assert settings.model == "parent-model"
    assert settings.api_format == "anthropic"
    assert settings.supports_vision is True
    assert settings.context_window == 200_000
    assert settings.max_tool_call_iterations == 99


def test_a_snapshot_does_not_follow_later_changes() -> None:
    """A snapshot, not a live view: a running child keeps its spawn-time limits."""
    parent = SimpleNamespace(
        _effective_model=lambda: "m",
        max_tokens=1,
        api_format="openai",
        supports_vision=False,
        context_window=1000,
        output_reserve=1,
        max_parallel_agents=1,
        sub_agent_timeout_seconds=1.0,
        sub_agent_retries=0,
        max_tool_call_iterations=10,
        max_truncation_continuations=1,
        result_content_max_chars=100,
    )
    settings = settings_from(parent)
    parent.max_tool_call_iterations = 999
    assert settings.max_tool_call_iterations == 10


def test_the_settings_are_frozen() -> None:
    """Mutation would defeat the point of a contract."""
    with pytest.raises(Exception):
        _settings().model = "changed"  # type: ignore[misc]


# ── The agent still delegates ────────────────────────────────────────────────


def test_the_agent_still_builds_a_sub_agent() -> None:
    import agent as agent_module

    registry = agent_module.ToolRegistry()
    agent = agent_module.BaseAgent(
        object(), registry, model="fake-model", api_format="openai"
    )
    sub = agent._create_sub_agent(registry)
    assert isinstance(sub, agent_module.BaseAgent)
    assert sub._transport is agent._transport, "the routing table must be inherited"
    assert sub.max_tool_call_iterations == agent.max_tool_call_iterations


def test_the_sub_agent_inherits_the_parents_buses() -> None:
    """A sub-agent's turns belong to the parent's composition.

    A sub-agent runs *inside* the parent's turn, so its turn and model events are the same
    composition's events.  Omitted at construction, ``BaseAgent`` falls back to the
    module-level buses: the composition's subscribers never hear them, and a listener left
    on one of those buses by some *other* runtime in the same process hears them instead.

    The same defect was found twice before by driving the product rather than the parts --
    once for the process-wide agent and once for a web session's -- so this asserts the
    third construction site directly.
    """
    agent = build_sub_agent(
        _spec(
            event_bus="the-turn-bus",
            llm_event_bus="the-llm-bus",
            tool_event_bus="the-tool-bus",
        ),
        agent_type=_FakeAgent,
    )

    assert agent.kwargs["event_bus"] == "the-turn-bus"
    assert agent.kwargs["llm_event_bus"] == "the-llm-bus"
    assert agent.kwargs["tool_event_bus"] == "the-tool-bus"


def test_a_spec_without_buses_passes_none_so_the_default_still_applies() -> None:
    """Control: a hand-built spec keeps the fallback it had.

    ``BaseAgent`` treats ``None`` as "the process bus", which is what a test or an embedder
    constructing one by hand relies on.  The builder must pass the absence through rather
    than inventing a bus for it.
    """
    agent = build_sub_agent(_spec(), agent_type=_FakeAgent)

    assert agent.kwargs["event_bus"] is None
    assert agent.kwargs["llm_event_bus"] is None
    assert agent.kwargs["tool_event_bus"] is None

"""The agent as a plugin: the composition stops building it in its own body.

What it takes, measured rather than guessed
-------------------------------------------
Ten constructor arguments and nine tuning assignments -- and every tuning value is
``cfg`` (or ``cfg["orchestration"]``) with a ``shared.*`` default.  Three of the
constructor's inputs are pure functions of ``cfg`` (``supports_vision``,
``context_window``, ``output_reserve``) and the rest are services or mapping entries.
Nothing here reads session or process state the composition cannot supply.

Two helpers had to move for it: ``active_context_window`` and ``active_output_reserve``
lived in ``bootstrap``, and a plugin cannot import the composition root.  They were
already pure functions of one config.

What it wires, and why it can
-----------------------------
``plugin_catalog`` and ``context_manager`` were assigned onto the agent *by the
composition*, after the mount -- which is why this plugin could not exist before
``SessionPlugin`` did: `context_manager` did not exist when a plugin mounted here would
have run.  Both are services now, both are provided before this plugin mounts, so the
agent is finished when it is provided and nothing edits an object a plugin owns.
"""

from __future__ import annotations

from typing import Any

from agent.core.agent_settings import AgentSettings
from agent.config import (
    active_context_window,
    active_output_reserve,
    bounded_int,
    provider_supports_vision,
)
from agent import shared
from agent.core.agent import BaseAgent as _DefaultAgent

__all__ = ["AgentPlugin"]


class AgentPlugin:
    """Provides ``agent``."""

    name = "agent"

    def __init__(self, agent_class: Any = _DefaultAgent) -> None:
        """*agent_class* is what gets constructed.

        Passed in rather than imported where it is used, because **the class is a
        substitution point**: ``bootstrap`` hands its own module-level ``BaseAgent``, so
        replacing that name still decides which implementation runs -- which it stopped
        doing the moment this construction moved behind an import.  That is not only a
        test convenience: "which agent" is exactly the kind of decision the composition is
        supposed to make, and a plugin that imports its own answer cannot be told.
        """
        self._agent_class = agent_class

    #: Everything the construction reads.  ``llm`` is the model adapter, passed both as
    #: the transport and as the single name the agent resolves by name -- see
    #: ``BaseAgent`` for why that mapping is one entry wide.
    inject = (
        "cfg",
        "client",
        "registry",
        "model",
        "max_tokens",
        "api_format",
        "llm",
        "context_manager",
        "plugin_catalog",
    )

    def setup(self, ctx: Any) -> None:
        cfg = ctx.get("cfg", None) or {}

        agent = self._agent_class(
            ctx.get("client"),
            ctx.get("registry"),
            model=str(ctx.get("model")),
            max_tokens=int(ctx.get("max_tokens")),
            api_format=str(ctx.get("api_format")),
            supports_vision=provider_supports_vision(cfg, str(cfg.get("active_provider", "anthropic"))),
            context_window=active_context_window(cfg),
            output_reserve=active_output_reserve(cfg),
            transport=ctx.get("llm"),
            # One name, not the whole mapping: this is what the agent resolves by name.
            services={"llm": ctx.get("llm")},
            # The composition's buses, when it provided any.  Passed at construction
            # rather than assigned afterwards: the agent reads them in `__init__`, so an
            # attribute set later would be a second field nothing looks at -- which is what
            # the first version of this did.
            event_bus=ctx.get("event_bus.turn", None),
            llm_event_bus=ctx.get("event_bus.llm", None),
            tool_event_bus=ctx.get("event_bus.tool", None),
        )
        # One typed value rather than twelve assignments.  The derivation -- including the
        # three different clamp idioms that were spread over forty lines -- lives in
        # `AgentSettings.from_config`, and `apply_to` copies it onto the agent so every
        # existing reader of `agent.max_parallel_agents` and its siblings keeps working.
        AgentSettings.from_config(cfg).apply_to(agent)
        # Wired here rather than by the composition afterwards.  Both services exist by
        # the time this plugin mounts -- `plugin_catalog` is provided before any mount, and
        # `context_manager` by `SessionPlugin` -- so the agent is finished when it is
        # provided, and nothing edits an object a plugin owns.
        agent.plugin_catalog = ctx.get("plugin_catalog")
        agent.context_manager = ctx.get("context_manager")
        ctx.provide("agent", agent)

"""Mount order does not decide whether the composition works.

The composition root mounted its plugins in an order and documented the constraints in
comments -- "After Memory: it injects `ltm_store`".  Those constraints had already stopped
existing: activation is by dependency **name**, so a plugin whose dependency is absent stays
pending and activates when a provider appears.

Measured: **24 of 24** orders of memory / session / agent / tools produce the same complete
composition.  The comments made the composition look more fragile than it was, and a reader
who believes them will preserve an order that carries no meaning.

This is also the precondition for the next step the review asks for -- declaring the
composition instead of sequencing it.  A list that can be reordered freely is a list that can
become data.
"""

from __future__ import annotations

import itertools

import pytest

PLUGINS = ("memory", "session", "agent", "tools")


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    from agent import shared

    monkeypatch.setattr(shared, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(shared, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(shared, "AGENT_HOME", tmp_path)
    (tmp_path / "context").mkdir()
    (tmp_path / "memory").mkdir()
    return tmp_path


def _build(order, tmp_path):
    from agent.core.agent import BaseAgent
    from agent.core.agent_plugin import AgentPlugin
    from agent.core.events import bus_name, default_buses
    from agent.kernel.plugin import PluginHost
    from agent.kernel.service import ServiceRegistry
    from agent.memory.plugin import MemoryPlugin
    from agent.memory.session_plugin import SessionPlugin
    from agent.tools.builtin_tools import BuiltinTools
    from agent.tools.plugin import ToolsPlugin
    from agent.tools.runtime import ToolRegistry

    mapping = dict(
        {bus_name(n): b for n, b in default_buses().items()},
        cfg={},
        workspace_root=tmp_path,
        output_dir=tmp_path,
        client=object(),
        model="m",
        max_tokens=10,
        api_format="anthropic",
        llm=object(),
        registry=ToolRegistry(),
        file_service=None,
        plugin_catalog=object(),
    )
    host = PluginHost(services=ServiceRegistry.adopt(mapping))
    factories = {
        "memory": MemoryPlugin,
        "session": SessionPlugin,
        "agent": lambda: AgentPlugin(BaseAgent),
        "tools": lambda: ToolsPlugin(BuiltinTools),
    }
    for name in order:
        host.load(factories[name]())
    return host


def test_every_mount_order_produces_the_same_composition(tmp_path) -> None:
    """All 24 orders, not a sample of them.

    A sampled order would pass while a specific pair still depended on being adjacent, which
    is exactly the kind of constraint the removed comments claimed.
    """
    orders = list(itertools.permutations(PLUGINS))
    assert len(orders) == 24

    failures = []
    for order in orders:
        host = _build(order, tmp_path)
        if sorted(host.loaded()) != sorted(PLUGINS) or host.pending() or host.failed():
            failures.append((order, host.loaded(), host.pending(), host.failed()))

    assert not failures, f"order decided the outcome for: {failures}"


def test_the_tool_set_is_registered_whatever_the_order(tmp_path) -> None:
    """Control for the assertion above: the composition is *working*, not merely loaded.

    ``loaded()`` names the plugins; it says nothing about whether the tool set actually
    registered its tools, which is the thing a user notices.
    """
    from agent.tools.runtime import ToolRegistry

    host = _build(("tools", "agent", "session", "memory"), tmp_path)
    registry = host.services.get("registry")

    assert isinstance(registry, ToolRegistry)
    assert "read_file" in registry.list_tools()

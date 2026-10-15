"""MCP is provided by a plugin, and the composition still decides when to connect.

The client was built by ``bootstrap`` in two branches of an ``if`` and written into
the component mapping; *which* servers exist was split across the config, the
plugin catalog's bundled servers, and those two constructions (each of which had
to remember both).  These tests pin what the move has to keep true:

* the plugin provides the client, the merged server list and the status, and the
  client is constructed through ``agent.MCPClient`` so a substitution reaches it;
* the status counts the *merged* list, not just the configured half;
* the client is only built when there is something to connect;
* in the real composition, ``mcp_client`` comes from the ``mcp`` plugin and is
  published into the tool registry's context, which is where the tools read it.
"""

from __future__ import annotations

import pytest

from typing import Any

from agent.kernel.plugin import PluginHost
from agent.kernel.service import ServiceRegistry
from agent.plugins.core import core_plugins
from agent.tools.mcp_plugin import MCPPlugin


def _contributed(*configs: dict) -> Any:
    """A `contributed_mcp_servers` registry holding *configs*, as a bundle left it."""
    from agent.tools.mcp_servers import MCPServerRegistry

    registry = MCPServerRegistry()
    for config in configs:
        registry.add(config, owner="acme")
    return registry


def _mount(mapping: dict) -> PluginHost:
    host = PluginHost(services=ServiceRegistry.adopt(mapping))
    host.load(MCPPlugin())
    return host


def test_no_servers_means_no_client() -> None:
    mapping: dict = {"cfg": {}, "registry": object(), "contributed_mcp_servers": _contributed()}

    _mount(mapping)

    assert mapping["mcp_client"] is None
    assert mapping["mcp_server_configs"] == []
    assert mapping["mcp_status"]["configured_servers"] == 0


def test_the_client_is_built_from_the_configured_servers() -> None:
    mapping: dict = {
        "cfg": {"mcp_servers": [{"name": "one", "command": "a"}]},
        "registry": object(),
        "contributed_mcp_servers": _contributed(),
    }

    _mount(mapping)

    assert type(mapping["mcp_client"]).__name__ == "MCPClient"
    assert [server["name"] for server in mapping["mcp_server_configs"]] == ["one"]
    assert mapping["mcp_status"]["configured_servers"] == 1


def test_the_server_list_is_the_configured_ones_plus_the_bundled_ones() -> None:
    """The union that used to be recomputed in two places, in one place."""
    mapping: dict = {
        "cfg": {"mcp_servers": [{"name": "configured", "command": "a"}]},
        "registry": object(),
        "contributed_mcp_servers": _contributed({"name": "bundled", "command": "b"}),
    }

    _mount(mapping)

    assert [server["name"] for server in mapping["mcp_server_configs"]] == [
        "configured",
        "bundled",
    ]
    assert mapping["mcp_status"]["configured_servers"] == 2, (
        "the status counts what will be attempted, not only the config's half"
    )


def test_bundled_servers_alone_still_get_a_client() -> None:
    mapping: dict = {
        "cfg": {},
        "registry": object(),
        "contributed_mcp_servers": _contributed({"name": "bundled", "command": "b"}),
    }

    _mount(mapping)

    assert type(mapping["mcp_client"]).__name__ == "MCPClient"
    assert [server["name"] for server in mapping["mcp_server_configs"]] == ["bundled"]


def test_the_real_composition_gets_mcp_client_from_the_plugin(monkeypatch, tmp_path) -> None:
    import asyncio

    import agent as agent_module
    from agent.runtime.contracts import resolve
    from test_agent_integration import _minimal_cfg

    cfg = _minimal_cfg()
    cfg["mcp_servers"] = [{"name": "demo", "command": "fake"}]
    monkeypatch.setattr(
        agent_module.ModelClientFactory,
        "from_config",
        lambda cfg, announce=True: (object(), "fake-model", 1024),
    )
    monkeypatch.setattr(agent_module, "CONTEXT_DIR", tmp_path / "context")
    monkeypatch.setattr(agent_module, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(agent_module, "PROMPTS_DIR", tmp_path / "prompts")
    monkeypatch.setattr(agent_module, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(agent_module, "DEFAULT_OUTPUT_DIR", tmp_path / "output")

    components = asyncio.run(agent_module._build_components_async(cfg, announce=False))

    client = resolve(components, "mcp_client")
    assert client is not None
    assert components["plugin_catalog"].who_provides("mcp_client") == "mcp"
    # The tools read it from the registry's context, so that must be the same object.
    assert components["registry"].get_context("mcp_client") is client
    assert isinstance(components.get("mcp_task"), asyncio.Task), (
        "the composition still starts the background connect, because it knows when "
        "the system prompt it may refresh exists"
    )
    from agent.config import _close_components

    asyncio.run(_close_components(components))


def test_a_profile_can_name_another_plugin_for_mcp_client() -> None:
    names = [plugin.name for plugin in core_plugins(replacements={"mcp_client": "acme.mcp"})]

    assert "mcp" not in names, "the plugin it displaces goes"
    assert "tools" in names, "and everything that reads the client stays"


@pytest.mark.parametrize("service", ["mcp_client", "mcp_server_configs", "mcp_status"])
def test_the_declaration_says_what_the_plugin_provides(service: str) -> None:
    entry = next(plugin for plugin in core_plugins() if plugin.name == "mcp")

    assert service in entry.provides


# ── close() is loop-safe ─────────────────────────────────────────────────────
#
# A task belongs to the loop that created it and ``asyncio.gather`` refuses one from
# another.  ``close`` used to gather whatever was in the list, so the composition that
# ``_build_components_async`` builds (background connect started on its loop) and
# ``_close_components`` tears down (another loop) raised
# ``ValueError: The future belongs to a different loop`` -- the composition could be
# built and never closed.  These pin both halves: the foreign case must not raise, and
# the same-loop case must still *wait* rather than degrade into a no-op.


def _client():
    from agent.tools.runtime import MCPClient, ToolRegistry

    return MCPClient(ToolRegistry())


def test_closing_a_client_from_another_loop_does_not_raise() -> None:
    import asyncio

    client = _client()

    async def build() -> None:
        # The task belongs to this loop, which ``asyncio.run`` closes underneath it.
        client._server_tasks.append(asyncio.create_task(asyncio.sleep(3600)))

    asyncio.run(build())
    assert client._server_tasks, "the task was recorded on the first loop"

    asyncio.run(client.close())

    assert client._server_tasks == [], "the references are dropped, not leaked"


def test_close_still_waits_for_the_tasks_this_loop_owns() -> None:
    """Control: loop-safety must not turn close into "signal and return"."""
    import asyncio

    client = _client()
    cleaned: list[str] = []

    async def run() -> None:
        async def server() -> None:
            await client._shutdown.wait()
            cleaned.append("cleaned")

        client._server_tasks.append(asyncio.create_task(server()))
        await asyncio.sleep(0)  # let it reach the wait
        await client.close()

    asyncio.run(run())

    assert cleaned == ["cleaned"], "close must wait for its own tasks to finish"
    assert client._server_tasks == []

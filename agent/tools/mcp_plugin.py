"""MCP is a plugin: it provides ``mcp_client``.

Why a plugin
------------
``mcp_client`` is a service the tools, the shutdown path and a plugin reload all
resolve by name, but it was built by ``bootstrap`` -- twice, in two branches of an
``if`` -- and written into the component mapping.  "Replace the MCP client" meant
editing the composition root, and *which* servers exist was split across three
places: the config, the plugin catalog's bundled servers, and the two
constructions that each had to remember both.

Which servers exist is one question, so it is answered in one place here: the
configured ones plus the ones other plugins contributed, read from the
``contributed_mcp_servers`` capability service the registry plugin provides.  That service is
provided *first* -- by a plugin that needs nothing (``agent/tools/mcp_servers.py``)
-- which is what makes the order work: the bundles that contribute are waiting for
it, the kernel activates them when it appears, and this plugin, which declares it in
``inject`` too, is activated last and therefore sees them.  (It used to read the
loader's ``_bundled_mcp`` list, which is why the plugin also injected
``plugin_catalog``.)  The client is built once and provided under the name everyone
already resolves, and the status the composition reports is provided beside it (its
``configured_servers`` count is the merged total, which is what the two old
``_empty_mcp_status`` calls were recomputing by hand).

What stays in the composition, and why
--------------------------------------
*When* to connect.  The background connect publishes a refreshed system prompt
when a server registers tools, so it must not run before that prompt exists -- and
that is a fact about the composition's own order, not about MCP.  The composition
starts it from the client and the merged list this plugin provides, at the point
it always did.
"""

from __future__ import annotations

from typing import Any

__all__ = ["MCPPlugin", "empty_mcp_status"]


def empty_mcp_status(configured_servers: int = 0) -> dict[str, int]:
    return {
        "configured_servers": configured_servers,
        "connected_servers": 0,
        "failed_servers": 0,
        "registered_tools": 0,
    }


class MCPPlugin:
    """Provides ``mcp_client``, ``mcp_server_configs`` and ``mcp_status``."""

    name = "mcp"

    #: The config names the configured servers; ``mcp_servers`` holds the contributed
    #: ones; the registry is what the client registers its tools on.
    inject = ("cfg", "registry", "contributed_mcp_servers")

    def setup(self, ctx: Any) -> None:
        import agent as agent_module

        from agent import shared

        cfg = ctx.get("cfg", None) or {}
        configured = list(cfg.get("mcp_servers", []) or [])
        contributed = list(ctx.get("contributed_mcp_servers").all())
        servers = configured + contributed

        client = None
        if servers:
            # Read through the package attribute, at setup time, exactly as the
            # composition did: substituting ``agent.MCPClient`` is how a test (or a
            # deployment) replaces this client, and a module-level import would
            # freeze the class before the substitution could reach it.
            client = agent_module.MCPClient(ctx.get("registry"))
        if configured:
            shared.CONSOLE.print(
                "[dim]MCP connecting in background: "
                f"{len(configured)} configured server(s)[/dim]"
            )

        ctx.provide("mcp_client", client)
        ctx.provide("mcp_server_configs", servers)
        ctx.provide("mcp_status", empty_mcp_status(len(servers)))

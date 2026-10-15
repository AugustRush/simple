"""MCP server configs as a capability a plugin contributes.

The same shape as ``agent/channels/registry.py`` and ``agent/skills/scoped.py``: a
registry that owns the records, and a per-plugin view that attributes what a plugin
adds and takes it back when that plugin's scope unwinds.

Why it is a plugin of its own
-----------------------------
The client plugin computes the merged server list in its ``setup`` (configured plus
contributed), and the plugins that *contribute* are discovered bundles -- they are
waiting for this service.  If the client plugin provided the registry too, the order
would be: client setup runs (registry appears, list computed while still empty) and only
then do the contributors activate.  So the registry is provided first, by a plugin that
needs nothing, and the client plugin declares ``inject = ("mcp_servers", ...)``: the
kernel activates the contributors when the registry appears, and the client last, which
is exactly the order the computation needs.
"""

from __future__ import annotations

from typing import Any

from agent.kernel.service import ScopedContribution

__all__ = ["MCPServerRegistry", "MCPServersPlugin", "ScopedMCPServers"]


class MCPServerRegistry:
    """The MCP server configs a composition will connect, in contribution order."""

    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []

    def add(self, config: dict, *, owner: str = "") -> Any:
        """Register one server config, returning the undo that removes it.

        By identity, like the channel registry: two bundles may legitimately declare a
        server with the same ``name`` (one shadows the other for the *client*), and an
        undo that removed by name would remove the wrong one.
        """
        record = {"config": config, "owner": owner}
        self._records.append(record)

        def forget() -> None:
            for index, item in enumerate(self._records):
                if item is record:
                    del self._records[index]
                    return

        return forget

    def all(self) -> "tuple[dict, ...]":
        return tuple(record["config"] for record in self._records)

    def records(self) -> "tuple[tuple[str, dict], ...]":
        """``(owner, config)`` pairs -- what a reload diffs to find newly declared ones."""
        return tuple((record["owner"], record["config"]) for record in self._records)

    def scoped(self, ctx: Any) -> "ScopedMCPServers":
        return ScopedMCPServers(self, ctx)


class ScopedMCPServers(ScopedContribution):
    """One plugin's handle on the registry."""

    __slots__ = ()

    def add(self, config: dict) -> dict:
        """Contribute one server config; it leaves when this plugin unloads."""
        self._contribute(self._registry.add(config, owner=self.owner))
        return config

    def __getattr__(self, item: str) -> Any:
        """Reads pass through: a view is the service *plus* attribution.

        The rule the skill catalog taught the hard way -- see
        ``docs/superpowers/specs/2026-10-13-capability-services-and-contribution-shape-design.md``.
        A capability service that grows a view must not change what its readers see.
        """
        return getattr(self._registry, item)


class MCPServersPlugin:
    """Provides ``contributed_mcp_servers``: the configs plugins contribute.

    Named for what it holds rather than for the MCP config field it is about: the
    *configured* servers are ``cfg["mcp_servers"]``, and a service with the same name as
    a config key makes every ledger scan read the config as a provision.
    """

    name = "mcp_servers"

    #: Nothing: a registry has no dependencies, and that is what lets contributors
    #: activate before the client that reads them.
    inject = ()

    def setup(self, ctx: Any) -> None:
        ctx.provide("contributed_mcp_servers", MCPServerRegistry())

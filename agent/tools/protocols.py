"""The tool layer's service protocols.

What this is for
----------------
``agent/memory/contracts.py`` is the template: a service whose surface is *written
down* can be checked (does this implementation have it?), replaced (is a second
implementation possible?), and discussed (which of these members does a caller
actually need?).  Every other service had an implicit surface -- whatever the call
sites happened to touch -- so "a plugin is an implementation of our protocol" was
unverifiable for them.

The protocols live next to the services they describe, one module per area, and the
ledger's ``contract`` column points at them
(:mod:`agent.runtime.core_services`).  That column is the map: a service with an
empty contract is a service whose interface nobody has written down yet.

What is in the protocol, and what is not
----------------------------------------
Exactly what a *consumer outside the implementation's own module* calls.  The
private helpers, the construction arguments and the collaborators a service builds
for itself stay out: they are the implementation's business, and a protocol that
named them would freeze an implementation detail as an interface.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

__all__ = ["MCPClientService"]


@runtime_checkable
class MCPClientService(Protocol):
    """The MCP client a composition provides as ``mcp_client``.

    Three members, because three are what anyone outside calls: the composition
    starts the background connect and reads the status it reports, and the shutdown
    path closes it.  Tool registration happens *inside* the client, against the tool
    registry it was built with -- that is why there is no ``register_tools`` here:
    the interface is what a caller does to it, not what it does on the way.
    """

    async def connect_from_config(
        self, config: dict, extra_env: "Optional[dict[str, str]]" = None
    ) -> None:
        """Connect every server in ``config["mcp_servers"]``.

        Failures are the implementation's to report: the composition proceeds with
        whichever servers came up (see ``agent/tools/mcp_plugin.py``).
        """

    def status_summary(self) -> "dict[str, Any]":
        """What the attempt did: configured, connected, failed, tools registered."""

    async def close(self) -> None:
        """Stop every server this client owns.  Safe to call once per process."""

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

__all__ = [
    "MCPClientService",
    "MCPServerContributionView",
    "MCPServerRegistryService",
    "ToolRegistryService",
]


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


@runtime_checkable
class ToolRegistryService(Protocol):
    """The tool registry a composition provides as ``registry``.

    Thirteen members, and the number is the point: this is the service every plugin
    that contributes a tool writes to, and every layer that runs a tool reads from, so
    its surface is wide by nature rather than by accident.  Writing it down is what
    makes "a plugin provides the registry" a claim with a shape -- a replacement has to
    accept that a *contribution* (`register`, `unregister_by_source_prefix`), a *view*
    (`scoped`, `derive`, `fork`) and a *read* (`list_tools`, `to_anthropic_format`,
    `call`, `tool_capabilities`, `tool_source`, `tools_with_capability`) are all part of
    it, and so is the context it carries for tool implementations
    (`get_context`/`set_context`).  ``registry.all()`` is *not* here: the only such call
    in the tree is on the **channel** registry, a different service with a same-named
    method -- verified at the call site, not inferred from the name.

    ``scoped(ctx)`` is the capability-view factory the kernel calls by name
    (``PluginContext._scoped`` tries it with ``getattr``), which is how ``ctx.tools``
    gives a plugin an attributed view whose registrations unwind with it.  It is in the
    protocol because an implementation without it silently loses that accounting --
    reads still work through the raw registry, which is what makes the loss silent.
    """

    def scoped(self, ctx: Any) -> Any:
        """A per-plugin view: registrations carry the plugin's name and unwind with it."""

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        fn: "Any",
        *,
        replace: bool = False,
        source: str = "runtime",
        capabilities: "Any" = None,
        authorizer: "Any" = None,
    ) -> None:
        """Add one tool.  The view adds ``owner``/``source``; the registry accepts them."""

    def unregister_by_source_prefix(self, prefix: str) -> None:
        """Remove every tool whose source starts with *prefix*."""

    def list_tools(self) -> "list[str]":
        """The names a composition currently offers."""

    def to_anthropic_format(self, names: "Any" = None) -> "list[dict]":
        """The provider-facing tool schemas, optionally limited to *names*."""

    async def call(self, tool_name: str, tool_input: dict) -> str:
        """Run one tool and return its text result."""

    def tool_capabilities(self, name: str) -> "Any":
        """The capabilities a tool declared (``orchestration``, ``filesystem``, ...)."""

    def tools_with_capability(self, capability: str) -> "list[str]":
        """Every tool that declared *capability*."""

    def tool_source(self, name: str) -> str:
        """Where a tool came from, for attribution and for the user-tool UI."""

    def set_context(self, key: str, value: Any) -> None:
        """Attach a value the tool implementations read at call time."""

    def get_context(self, key: str, default: Any = None) -> Any:
        """Read one back."""

    def derive(self, view: "Any", context: "Any" = None) -> "ToolRegistryService":
        """A registry restricted to a named tool view."""

    def fork(
        self, context: "Any" = None, *, exclude_source_prefixes: "tuple[str, ...]" = ()
    ) -> "ToolRegistryService":
        """A registry with its own context and no inherited tools -- a session's own."""


@runtime_checkable
class MCPServerContributionView(Protocol):
    """One plugin's handle on the MCP server registry.

    A bundle that ships a ``.mcp.json`` (or names one in its manifest) contributes it
    here, in its ``setup``: the view attributes the server to the plugin and takes it
    back when the plugin unloads.  Same shape as the channel and skill views -- one
    contribution mechanism, four capability services.
    """

    def add(self, config: dict) -> dict:
        """Contribute one server config, returning it."""


@runtime_checkable
class MCPServerRegistryService(Protocol):
    """The MCP server registry a composition provides as ``contributed_mcp_servers``."""

    def all(self) -> "tuple[dict, ...]":
        """Every contributed server config, in contribution order."""

    def records(self) -> "tuple[tuple[str, dict], ...]":
        """``(owner, config)`` pairs -- what ``plugin reload`` diffs."""

    def scoped(self, ctx: Any) -> MCPServerContributionView:
        """The per-plugin view (the kernel calls this by name for ``ctx.contributed_mcp_servers``)."""

"""Tools as a capability a plugin contributes, with the undo bound to its context.

The plan's model is ``Plugin → Service / Event / Capability``, where a *capability*
is what a **service** exposes as callable -- tools, commands, model-facing actions --
and not a universal abstraction every part of the system is squeezed into.  A tool is
therefore not a kind of plugin; it is something a plugin registers *on a service*, and
this module is the service's answer to "who is registering, and how does it come back
out".

Why the view is bound to a context
----------------------------------
``ToolRegistry.register`` takes a ``source`` string, and removal is
``unregister_by_source_prefix(source)``.  That works, and it requires every caller to
invent a source name and remember to unregister -- which is the bookkeeping a scope
exists to remove.  Reached through ``ctx.tools``, the source is the plugin's name,
derived rather than invented, and the removal is registered on the plugin's own
lifetime at the moment of registration.

So the two halves cannot drift: there is no path that adds a tool without also
recording how to take it away, because both happen in one function.

What is deliberately not here
-----------------------------
No ``Capability`` base class and no ``execute()``.  The plan is explicit that a tool
(``input → output``), a model (``streaming inference``), a session (``persistent
state``) and a loop (``execution driver``) are not the same shape, and forcing one
interface on them loses the type semantics that make each usable.  A tool is a
callable with a schema; that is the whole of its contract here.
"""

from __future__ import annotations

from typing import Any, Callable

__all__ = ["ScopedTools"]


class ScopedTools:
    """One plugin's handle on the tool registry.

    Built by :meth:`agent.tools.runtime.ToolRegistry.scoped`, which the kernel calls
    when a plugin reaches ``ctx.tools``.  Not constructed directly.
    """

    __slots__ = ("_registry", "_ctx")

    def __init__(self, registry: Any, ctx: Any) -> None:
        self._registry = registry
        self._ctx = ctx

    # -- identity ------------------------------------------------------------

    @property
    def owner(self) -> str:
        """The plugin these tools belong to; also the registry's ``source``."""
        return f"plugin:{self._ctx.name}"

    # -- contributing --------------------------------------------------------

    def register(
        self,
        tool: Any = None,
        *,
        name: str | None = None,
        description: str | None = None,
        parameters: dict | None = None,
        fn: Callable[..., Any] | None = None,
        capabilities: Any = None,
        replace: bool = True,
    ) -> str:
        """Contribute a tool, removed when this plugin unloads.

        Two shapes, because both are in use: pass a **tool object** (anything with
        ``name``/``fn``) as the plan's example does, or pass the registry's own
        keyword arguments.  The object form reads
        ``ctx.tools.register(CreateIssue(github))``.

        ``replace`` defaults to *True* here, unlike the registry's own default.  A
        plugin re-registering on reload is the ordinary case -- the catalog reloads,
        the plugin registers the same tool again -- and the registry's refusal to
        replace exists to catch two *different* sources claiming a name.  A plugin
        claiming its own name again is not that, and the alternative is every plugin
        author writing ``replace=True`` to say so.

        Returns the tool's name, so a caller can log or unregister it without
        repeating the string.
        """
        if tool is not None:
            name = name or getattr(tool, "name", None)
            description = (
                description
                if description is not None
                else (getattr(tool, "description", "") or "")
            )
            parameters = (
                parameters
                if parameters is not None
                else (getattr(tool, "parameters", None) or {"type": "object", "properties": {}})
            )
            fn = fn if fn is not None else getattr(tool, "fn", tool)
            capabilities = (
                capabilities
                if capabilities is not None
                else getattr(tool, "capabilities", None)
            )
        if not name:
            raise ValueError(
                "a contributed tool needs a name: pass a tool object with one, or "
                "name=<str>"
            )

        self._registry.register(
            name,
            description or "",
            parameters or {"type": "object", "properties": {}},
            fn,
            source=self.owner,
            capabilities=capabilities,
            replace=replace,
        )
        # By source prefix rather than by name: one plugin may contribute several
        # tools, and the prefix is what identifies them as a group.
        self._ctx.on_unwind(lambda: self._registry.unregister_by_source_prefix(self.owner))
        return name

    # -- reading -------------------------------------------------------------

    def names(self) -> tuple[str, ...]:
        """The tools this plugin contributed."""
        return tuple(
            name
            for name, tool in self._registry._tools.items()
            if getattr(tool, "source", "") == self.owner
        )

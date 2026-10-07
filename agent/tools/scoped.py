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
        tool_or_name: Any = None,
        description: str | None = None,
        parameters: dict | None = None,
        fn: Callable[..., Any] | None = None,
        *,
        name: str | None = None,
        capabilities: Any = None,
        replace: bool = True,
        source: str | None = None,
        authorizer: Callable[..., Any] | None = None,
    ) -> str:
        """Contribute a tool, removed when this plugin unloads.

        Two calling conventions, because this view stands where the registry stands.  A
        **tool object** as the first argument is the plan's form --
        ``ctx.tools.register(CreateIssue(github))`` -- and the registry's own positional
        form (``name, description, parameters, fn``) is what code written against
        ``ToolRegistry`` uses.  Refusing the second would make this view *almost* a
        registry, which is the kind of near-miss that only shows up when somebody swaps
        one for the other.

        ``replace`` defaults to *True*, unlike the registry's own default: a plugin
        re-registering on reload is ordinary, and the registry's refusal exists to catch
        two *different* sources claiming a name, which a plugin claiming its own is not.

        ``source`` overrides the derived ``plugin:<name>``.  A caller that already has one
        is not contributing -- the agent's ``spawn_agent`` carries ``runtime:spawn`` so the
        runtime's tools can be removed by prefix -- and overwriting it with the plugin's
        name would silently re-tag it.
        """
        if isinstance(tool_or_name, str):
            name = tool_or_name
            tool = None
        else:
            tool = tool_or_name

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
                else (
                    getattr(tool, "parameters", None)
                    or {"type": "object", "properties": {}}
                )
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

        # The registry's **whole** keyword set, forwarded.  A view that accepts most of
        # it is a view that fails the first time somebody hands it a call written against
        # the registry -- which has now happened twice (`source`, then `authorizer`), each
        # time discovered by a plugin failing to load rather than by a test.  The guard in
        # `tests/test_tool_capabilities.py` compares the two signatures so the third one is
        # a test failure instead.
        self._registry.register(
            name,
            description or "",
            parameters or {"type": "object", "properties": {}},
            fn,
            source=source or self.owner,
            capabilities=capabilities,
            replace=replace,
            authorizer=authorizer,
        )
        # By source prefix rather than by name: one plugin may contribute several tools,
        # and the prefix is what identifies them as a group.  An explicit `source` is that
        # prefix too, so a caller that tagged its own tools gets them removed together.
        self._ctx.on_unwind(
            lambda: self._registry.unregister_by_source_prefix(source or self.owner)
        )
        return name

    # -- reading -------------------------------------------------------------

    def get_context(self, key: str, default: Any = None) -> Any:
        """Read the registry's shared context, through the view.

        Forwarded because tool *implementations* take the registry and read this:
        ``BuiltinTools`` calls ``self.registry.get_context("output_dir")`` and four more
        like it, so a view without this is not a registry and the built-in tool set cannot
        be handed one -- which is what stopped the tool set from being a plugin.

        **Reads, not writes.**  ``set_context`` is deliberately absent: it is how the
        *composition* configures the process (``output_dir``, ``file_access_policy``), and
        a plugin changing it would change it for every other plugin.  A view that forwarded
        writes would make "one plugin's handle" a way to reach into everyone else's.
        """
        return self._registry.get_context(key, default)

    def names(self) -> tuple[str, ...]:
        """The tools this plugin contributed."""
        return tuple(
            name
            for name, tool in self._registry._tools.items()
            if getattr(tool, "source", "") == self.owner
        )

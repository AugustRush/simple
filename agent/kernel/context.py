"""The context a plugin is handed, and the whole of its vocabulary.

Six methods, and the plan's sentence is the specification: *let plugins be
composed, depend on each other, provide services, listen to events, and be
unloaded or replaced.*

``plugin(p)``
    Load another plugin.  Its install is recorded on this context's scope, so it is
    unloaded with the plugin that loaded it.
``provide(name, value)``
    Offer a service for the lifetime of the scope.  Registered as an undo, so it
    disappears when the scope does.
``get(name, default)``
    Resolve a service.  With no default a missing name raises -- a silent ``None``
    is how an absent dependency becomes an ``AttributeError`` three frames away
    from the thing that was actually missing.
``on(event_type, handler)``
    Subscribe.  Returns an unsubscribe callable *and* records it on the scope, so
    unloading cannot leave a listener behind.
``emit(event)``
    Notify listeners of an event's type.
``scope()``
    A nested scope, for a plugin that wants to register something it can reverse
    earlier than its own unload.

What is deliberately missing
----------------------------
No ``ctx.agent``, ``ctx.tools``, ``ctx.sessions``.  Those are services, reached
through ``get`` (or, better, declared in ``inject`` and reached as attributes --
see below).  A named accessor per service would put the service vocabulary in the
kernel, which is exactly the coupling this design removes.

The one convenience: injected names become attributes
-----------------------------------------------------
A plugin that declares ``inject = ("tools",)`` gets ``ctx.tools``.  That is not a
privileged name -- it is the same ``get`` under the declared name -- and it exists
because the alternative is the "grab the whole runtime and drill down" pattern the
plan calls out by name:

    plugin.runtime.agent.memory.tool_registry   # no
    ctx.tools                                   # yes
"""

from __future__ import annotations

from typing import Any, Callable

from agent.kernel.service import (
    MissingServiceError,
    ServiceRegistry,
    ServiceScope,
)

_MISSING = object()

__all__ = ["Context"]


class Context:
    """A plugin's handle on the kernel.

    Bound to one :class:`~agent.kernel.plugin.Install`: its scope is where every
    registration goes, and its name is what a diagnostic reports.
    """

    __slots__ = ("_host", "_install", "_extra", "_views", "_domain_lookup")

    def __init__(self, *, host: Any, install: Any) -> None:
        self._host = host
        self._install = install
        #: ``name -> (service, view)``, so a scoped view is built once and is only
        #: reused while the underlying service is the same object.
        self._views: dict[str, tuple[Any, Any]] = {}
        #: Names the injectable-attribute shortcut should not shadow.  A service
        #: called ``get`` would otherwise replace this class's own method.
        self._extra = frozenset(Context.__slots__) | frozenset(
            n for n in dir(Context) if not n.startswith("__")
        )
        #: Asked by :meth:`on` whether an event type belongs to a domain -- and therefore
        #: to a different bus.  Supplied by whoever wired the host rather than imported
        #: here: a domain is a set of event types kept **outside** the kernel by design,
        #: and the kernel reaching for the table would be the coupling it exists to
        #: prevent.  ``None`` means "no table wired", and then ``on`` accepts anything.
        self._domain_lookup = getattr(host, "domain_lookup", None)

    # -- identity ------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._install.name

    @property
    def services(self) -> ServiceRegistry:
        """The registry behind ``get``, for a caller that needs membership tests."""
        return self._host.services

    # -- services ------------------------------------------------------------

    def provide(self, name: str, value: Any, *, replace: bool = False) -> None:
        """Offer *name* to the whole composition, for as long as this plugin lives.

        Recorded on the scope, so unloading removes it and dependents waiting on it
        are taken down with it.
        """
        self._install.scope.provide(name, value, replace=replace)
        # Told, not inferred: only a *newly* offered service can unblock a waiter,
        # so the host re-checks pending plugins after a setup that provided one --
        # rather than after every load, which would activate a plugin the moment an
        # unrelated plugin appeared.
        self._install.provided = True
        if name not in self._install.provided_names:
            self._install.provided_names.append(name)

    def get(self, name: str, default: Any = _MISSING) -> Any:
        """Resolve a service, raising for a missing name unless given a default.

        The result is **scoped to this plugin** when the service supports it: a
        service that exposes ``scoped(ctx)`` is asked for a per-plugin view, and a
        mutation made through that view registers its own undo on this plugin's
        lifetime.  That is what makes ``ctx.tools.register(tool)`` reversible
        without the plugin managing a scope -- see :meth:`_scoped`.

        A service that does not expose ``scoped`` is returned as-is, so a plain
        read-only service needs no ceremony and keeps its identity for an
        ``is`` check or an ``isinstance`` test.
        """
        service = (
            self.services.get(name) if default is _MISSING
            else self.services.get(name, default)
        )
        return self._scoped(name, service)

    def _scoped(self, name: str, service: Any) -> Any:
        """A per-plugin view of *service*, cached, or the service itself.

        The factory is handed **this context**, not a bare scope, because a
        contribution usually has to be attributable: the plugin catalog keys slash
        commands and bundled skills by plugin name, and a view holding only a scope
        could not say whose they are.  It still reaches the scope through
        ``ctx.on_unwind``, so the view does not have to know how a scope works.

        Cached because a plugin reaches a service many times in a turn and a fresh
        view per lookup would be waste; and because a view compares by identity with
        itself, which is what lets a plugin hold one.
        """
        factory = getattr(service, "scoped", None)
        if not callable(factory):
            return service
        cached = self._views.get(name)
        if cached is not None and cached[0] is service:
            return cached[1]
        view = factory(self)
        self._views[name] = (service, view)
        return view

    def has(self, name: str) -> bool:
        return self.services.has(name)

    @property
    def bus(self) -> Any:
        """The event bus, for a subscriber that wants a set of types at once.

        Exposed rather than wrapped: a *domain* is a vocabulary of event types,
        and this codebase keeps those in ``agent/core/event_domains.py`` -- outside
        the kernel on purpose.  A plugin that wants "every turn event" asks that
        table to subscribe it to ``ctx.bus``, so the kernel never learns what a
        turn is.
        """
        return self._host.events

    # -- events --------------------------------------------------------------

    def on(self, event_type: type, handler: Callable[[Any], Any]) -> Callable[[], None]:
        """Subscribe *handler* to *event_type*; undone when this plugin unloads.

        For events the composition defines.  A **domain's** events are published on that
        domain's own bus, so this refuses them rather than installing a listener that is
        never called -- see :func:`agent.core.event_domains.domain_of`.  The refusal is
        the whole point: `ctx.on(TurnCompleted, handler)` reads correctly, registers
        successfully, and does nothing, which is indistinguishable from working code.

        The kernel does not import the domain table to ask; the lookup is passed in by
        whoever wired the host, because a domain is vocabulary that lives *outside* the
        kernel by design.
        """
        declared = self._domain_lookup(event_type) if self._domain_lookup else None
        if declared is not None:
            raise ValueError(
                f"{event_type.__name__} belongs to the {declared.name!r} domain, whose "
                f"events are published on that domain's bus, not on the context's. "
                f"Registering here would subscribe you to a bus nothing publishes to. "
                f"Use Domain.attach(ctx, handler) instead -- "
                f"from agent.core.event_domains import {declared.name.upper()}"
            )
        unsubscribe = self._host.events.on(event_type, handler)
        self._install.scope.on_unwind(unsubscribe)
        return unsubscribe

    async def emit(self, event: Any) -> Any:
        """Notify listeners of ``type(event)``."""
        return await self._host.events.emit(event)

    async def emit_blocking(self, event: Any) -> Any:
        """Notify listeners until one refuses; see ``EventBus.emit_blocking``."""
        return await self._host.events.emit_blocking(event)

    # -- composition ---------------------------------------------------------

    def plugin(self, plugin: Any) -> Any:
        """Load *plugin* as a child of this one.

        The child's install is recorded on this plugin's scope, so unloading the
        parent disposes the child: a cascade that is a consequence of the disposal
        rule rather than a feature layered on top of it.
        """
        install = self._host.load(plugin, parent=self._install.name)
        self._install.scope.on_unwind(lambda: self._unload_child(install.name))
        return install

    def _unload_child(self, name: str) -> None:
        """Remove a child that is still loaded.  Safe to call after it has gone."""
        if name in self._host.installs or name in self._host.pending():
            self._host.unload(name)

    # -- scopes --------------------------------------------------------------

    def on_unwind(self, handler: Callable[[], None]) -> None:
        """Register an undo on *this plugin's* lifetime.

        The general form, and the one a plugin usually wants: a registration that
        is not a service and not an event subscription -- a resource, a file, a
        domain subscription -- still has to be reversible, and the alternative is
        for the plugin to remember to unregister, which is the question this design
        exists to remove.

        Distinct from :meth:`scope`, which returns a *nested* scope for something a
        plugin wants to reverse earlier than its own unload.  Registering on a
        nested scope that nobody disposes is a silent no-op, which is why the two
        are separate calls rather than one.
        """
        self._install.scope.on_unwind(handler)

    def scope(self) -> ServiceScope:
        """A nested scope, reversible before this plugin unloads.

        Everything registered through the returned scope is undone when *it* is
        disposed, which is what a plugin wants when it manages something for a
        shorter period than its own lifetime.  For the common case -- "register
        this for as long as I am loaded" -- use :meth:`on_unwind`.
        """
        return self.services.scope()

    # -- injected attributes -------------------------------------------------

    def __getattr__(self, item: str) -> Any:
        """``ctx.tools`` for a plugin that declared ``inject = ("tools",)``.

        Only reached when normal attribute lookup fails, so a service cannot
        shadow a real method.  A name that was neither injected nor provided raises,
        which is the point: reaching for a service you did not declare is the
        coupling ``inject`` exists to make visible.
        """
        if item.startswith("_") or item in self._extra:
            raise AttributeError(item)
        if item in self._install.inject:
            try:
                # Through ``get``, not the registry, so an injected attribute is
                # the same scoped view a ``ctx.get(name)`` would return.  Reading
                # the registry directly here would hand over the raw service and
                # silently drop the per-plugin accounting.
                return self.get(item)
            except MissingServiceError as exc:
                raise AttributeError(
                    f"{self._install.name!r} injected {item!r} but it is not "
                    f"available; the plugin should not have activated"
                ) from exc
        raise AttributeError(
            f"{item!r} is not a method of Context and {self._install.name!r} did "
            f"not declare it in inject; add it to inject to use it as an attribute"
        )

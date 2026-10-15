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
    Subscribe to one event **class**, on whichever bus publishes it (the composition
    says which -- see :meth:`Context.on`).  Returns an unsubscribe callable *and*
    records it on the scope, so unloading cannot leave a listener behind.
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

from typing import Any, Awaitable, Callable, Iterator, Mapping

from agent.kernel.service import (
    MissingServiceError,
    ReservedServiceError,
    ServiceScope,
    UndeclaredServiceError,
)

_MISSING = object()

__all__ = ["Context"]


class _DeclaredServices(Mapping[str, Any]):
    """The registry as a plugin may see it: read-only, and only for declared names.

    Handed out by :attr:`Context.services`.  It replaces the raw
    :class:`~agent.kernel.service.ServiceRegistry`, which was writable and
    unrestricted: a plugin could ``ctx.services.provide("x", v)`` and register
    without an undo -- breaking "every registration is an effect" in one line --
    or resolve any name the composition happened to leave in the mapping, which
    made ``inject`` advisory rather than the dependency declaration the
    unload/replace cascade relies on.

    A ``Mapping`` so the three shapes :func:`~agent.kernel.service.resolve` and
    :func:`~agent.kernel.service.require_service` accept keep working, and with
    ``require``/``lookup`` spelled out because those two helpers prefer them.
    """

    __slots__ = ("_ctx",)

    def __init__(self, ctx: "Context") -> None:
        self._ctx = ctx

    def _read(self, name: str) -> Any:
        self._ctx.require_declared(name)
        self._ctx.require_allowed(name, "read")
        return self._ctx._host.services.get(name)

    def __getitem__(self, key: str) -> Any:
        return self._read(key)

    def get(self, key: str, default: Any = None) -> Any:
        self._ctx.require_declared(key)
        self._ctx.require_allowed(key, "read")
        return self._ctx._host.services.get(key, default)

    #: The names :func:`resolve` / :func:`require_service` look for first.
    def lookup(self, key: str, default: Any = None) -> Any:
        return self.get(key, default)

    def require(self, key: str) -> Any:
        return self._read(key)

    def has(self, key: str) -> bool:
        """Membership, deliberately **not** declaration-checked.

        Asking whether a name exists is how a plugin or a diagnostic decides what
        to report; it is not a read of the value, and refusing it would make
        ``pending_report``-style code unable to say what the composition holds.
        """
        return self._ctx._host.services.has(key)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self.has(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._ctx._host.services.names())

    def __len__(self) -> int:
        return len(self._ctx._host.services.names())


class Context:
    """A plugin's handle on the kernel.

    Bound to one :class:`~agent.kernel.plugin.Install`: its scope is where every
    registration goes, and its name is what a diagnostic reports.
    """

    __slots__ = ("_host", "_install", "_extra", "_views", "_bus_lookup", "_services_view")

    def __init__(self, *, host: Any, install: Any) -> None:
        self._host = host
        self._install = install
        #: ``name -> (service, view)``, so a scoped view is built once and is only
        #: reused while the underlying service is the same object.
        self._views: dict[str, tuple[Any, Any]] = {}
        #: The read-only view handed out by :attr:`services`, built on first ask.
        self._services_view: Any = None
        #: Names the injectable-attribute shortcut should not shadow.  A service
        #: called ``get`` would otherwise replace this class's own method.
        self._extra = frozenset(Context.__slots__) | frozenset(
            n for n in dir(Context) if not n.startswith("__")
        )
        #: ``(event_type, ctx) -> bus | None``, asked by :meth:`on` so a subscription
        #: lands on the bus that publishes the type.  Supplied by whoever wired the host
        #: rather than imported here: a domain is a set of event types kept **outside**
        #: the kernel by design, and the kernel reaching for that table would be the
        #: coupling it exists to prevent.  ``None`` means "no table wired", and then every
        #: type goes to the host's own bus -- the behaviour an embedder that provides
        #: nothing already had.
        self._bus_lookup = getattr(host, "event_bus_lookup", None)

    # -- identity ------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._install.name

    @property
    def services(self) -> "_DeclaredServices":
        """The registry as **reads of declared names**: read-only, check-enforced.

        Cached so a caller that holds it keeps one object, and so the identity
        comparison a test may make does not depend on how many times it asked.
        """
        if self._services_view is None:
            self._services_view = _DeclaredServices(self)
        return self._services_view

    def require_declared(self, name: str, action: str = "read") -> None:
        """Refuse a name this plugin never declared.

        The declaration (``inject`` for required, ``optional`` for tolerated) is
        what ``unload`` and ``replace`` walk; a read outside it is a dependency the
        kernel cannot see, which is how a replaced service leaves its reader
        holding the object whose owner is gone.  Enforced here rather than trusted
        because the failure is silent and delayed.

        A name this plugin *provided* counts as declared: offering it and reading
        it back is one act, and the second half cannot be a dependency on somebody
        else.  That is also what lets a plugin offer a default and honour an
        incumbent -- provide if absent, then read what the composition now holds.
        """
        install = self._install
        if (
            name in install.inject
            or name in install.optional
            or name in install.provided_names
        ):
            return
        raise UndeclaredServiceError(
            install.name,
            name,
            action=action,
            declared=tuple(install.inject),
            optional=tuple(install.optional),
        )

    def require_allowed(self, name: str, action: str = "read") -> None:
        """Refuse a name that is not part of the composition's plugin vocabulary.

        Declaration and vocabulary are two different questions, and a closed
        vocabulary needs both: ``inject`` says *this plugin* may depend on a name,
        and this says the name is a plugin's to depend on at all.  Composition
        internals share the adopted mapping with real services, so without the
        second check a plugin can resolve ``mcp_task`` or a session's staged
        components -- values with no contract and no lifetime a plugin can reason
        about.

        No policy wired means no vocabulary, which is the permissive behaviour an
        embedder that provides nothing already has.
        """
        policy = getattr(self._host, "service_policy", None)
        if policy is None or policy(name, action):
            return
        raise ReservedServiceError(self._install.name, name, action=action)

    # -- services ------------------------------------------------------------

    def provide(self, name: str, value: Any, *, replace: bool = False) -> None:
        """Offer *name* to the whole composition, for as long as this plugin lives.

        Recorded on the scope, so unloading removes it and dependents waiting on it
        are taken down with it.

        A wired ``service_validator`` is asked **before** anything is recorded: a
        service whose declaration the value does not satisfy is refused here, where the
        mistake was made and with a message that names the plugin, the name and the
        protocol -- rather than three frames away, at the first caller, as an
        ``AttributeError``.  Validating after the write would leave a refused value
        registered, which is the half-applied state the kernel refuses elsewhere.
        """
        self.require_allowed(name, "provide")
        validator = getattr(self._host, "service_validator", None)
        if validator is not None:
            validator(name, value)
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

        *name* must be declared (``inject`` or ``optional``); see
        :meth:`require_declared`.  For the service **itself** rather than this
        plugin's view of it, use :meth:`raw`.
        """
        self.require_declared(name)
        self.require_allowed(name, "read")
        service = (
            self._host.services.get(name) if default is _MISSING
            else self._host.services.get(name, default)
        )
        return self._scoped(name, service)

    def raw(self, name: str, default: Any = _MISSING) -> Any:
        """The service **itself**, not this plugin's scoped view.

        The counterpart of :meth:`get` for a plugin that *configures* a service
        rather than contributing to it.  ``ToolRegistry`` is the worked example:
        every plugin that adds a tool wants the view (attribution plus undo), and
        the one plugin that sets the registry's turn context wants the registry --
        given the view it raised ``'ScopedTools' object has no attribute
        'set_context'``, before any model call.

        Declared exactly like ``get``, so the dependency graph is unaffected by the
        choice of view.
        """
        self.require_declared(name)
        self.require_allowed(name, "read")
        if default is _MISSING:
            return self._host.services.get(name)
        return self._host.services.get(name, default)

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
        """Whether the composition holds *name*.  Not a read; see the view."""
        return self._host.services.has(name)

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
        """Subscribe *handler* to *event_type*, on **whichever bus publishes it**.

        The type is the channel, and the composition says where a type is published.  For
        an event a plugin or a composition defines itself that is ``ctx.bus``; for a
        **domain's** events -- the set this codebase keeps in
        ``agent/core/event_domains.py``, one bus per domain -- it is that domain's bus,
        resolved by the lookup whoever wired the host supplied.  So the obvious call is
        the correct one:

            ctx.on(ToolPreExecute, self.gate)          # one type
            TOOL.attach(ctx, self.watch)               # every event of a domain

        **Asking rather than refusing is the point.**  The kernel cannot work out on its
        own where ``TurnCompleted`` is published -- a domain is vocabulary kept *outside*
        the kernel by design -- so this used to raise and name ``Domain.attach``.  That
        refusal was right about the danger ("registered, and never called, which is
        indistinguishable from working code") and wrong about the remedy: the composition
        *does* know, and a subscription it can place is strictly better than one it
        rejects.  The danger is removed by construction instead of by a guard a plugin
        author has to be told about.

        What is still the composition's job: wiring the lookup, and publishing on the same
        bus.  A missing lookup means the kernel keeps its own default, which is the
        behaviour an embedder that provides nothing already had.
        """
        bus = self._bus_lookup(event_type, self) if self._bus_lookup else None
        unsubscribe = (bus if bus is not None else self._host.events).on(
            event_type, handler
        )
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

    def on_unwind_async(self, handler: Callable[[], Awaitable[None]]) -> None:
        """Register an *awaitable* undo on this plugin's lifetime.

        For what a synchronous handler cannot release: a connection pool, a subprocess
        that has to be waited for, a buffer that has to be flushed over the network.

        It is a **separate** call rather than an overload of :meth:`on_unwind` because the
        two run at different times.  ``unload`` is synchronous and stays that way -- making
        it async would change every caller -- so it runs the synchronous handlers and
        **hands back** these; whoever owns the event loop awaits them, and
        :meth:`PluginHost.pending_async_disposals` names anything still waiting.

        That last part is the point.  A teardown that is quietly never awaited is the
        failure this design exists to prevent, so an unawaited one is *visible* here rather
        than assumed to have happened.
        """
        self._install.scope.on_unwind_async(handler)

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
        if (
            item in self._install.inject
            or item in self._install.optional
            or item in self._install.provided_names
        ):
            try:
                # Through ``get``, not the registry, so an injected attribute is
                # the same scoped view a ``ctx.get(name)`` would return.  Reading
                # the registry directly here would hand over the raw service and
                # silently drop the per-plugin accounting.
                return self.get(item)
            except MissingServiceError as exc:
                raise AttributeError(
                    f"{self._install.name!r} declared {item!r} but it is not "
                    f"available; the plugin should not have activated"
                ) from exc
        raise AttributeError(
            f"{item!r} is not a method of Context and {self._install.name!r} did "
            f"not declare it in inject or optional; add it to use it as an attribute"
        )

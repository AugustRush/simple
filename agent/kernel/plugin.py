"""Plugins, their install records, and the host that owns the tree.

A plugin is three things
------------------------
``name``
    Identity.  Used for lookup, for the dependency graph, and for reporting.
``inject``
    A tuple of service names this plugin needs before it can activate.
``setup(ctx)``
    Where it registers.  Optionally ``teardown()`` for what a scope cannot own
    (a subprocess handle, a lock).

That is the whole protocol.  Notably it does **not** include a list of
capabilities: what a plugin contributes is whatever its ``setup`` registers, and
the scope is what makes those contributions reversible.  A declaration that could
disagree with the registrations would be a second source of truth.

Registrations are effects
-------------------------
Every registration a plugin makes goes through its :class:`~agent.kernel.service.ServiceScope`:
services it provides, listeners it subscribes, and the plugins it loads all record
an undo on that scope.  Disposing the scope is therefore the whole of unloading --
there is no per-kind teardown to keep in sync, which is what makes "who
unregisters this?" a question that cannot be asked here.

Composition is a plugin loading a plugin
----------------------------------------
``ctx.plugin(other)`` is how a tree is built.  The child's scope is a *record on
the parent's scope*, so unloading the parent disposes the child without any special
case -- a cascade that is a consequence of the disposal rule rather than a feature
layered on top of it.

Dependencies wait rather than fail
----------------------------------
A plugin whose ``inject`` names an absent service does not error; it stays
**pending**.  When some other plugin provides the name it activates.  That is what
makes load order stop mattering, and it is why a pending install holds no
registrations at all -- there is nothing half-loaded to reason about.
"""

from __future__ import annotations

import inspect

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol, runtime_checkable

from agent.kernel.event import EventBus
from agent.kernel.service import ServiceRegistry, ServiceScope

logger = logging.getLogger(__name__)

__all__ = ["Plugin", "Install", "PluginHost", "MissingNameError"]


class MissingNameError(ValueError):
    """A plugin with no ``name`` cannot be looked up, unloaded, or depended on."""


@runtime_checkable
class Plugin(Protocol):
    """What the host requires of a plugin.

    Structural rather than a base class: a plugin does not have to import the
    kernel to be one, which keeps the dependency pointing the right way.
    """

    name: str
    inject: tuple[str, ...]

    def setup(self, ctx: Any) -> None: ...


@dataclass
class Install:
    """One plugin's presence in the tree.

    ``pending`` means the plugin is known but not active: its ``setup`` has not run,
    so it holds no registrations and its scope is empty.  ``parent`` is the install
    that loaded it, or ``None`` for a root -- recorded so a diagnostic can say
    *why* a plugin is present rather than only that it is.
    """

    name: str
    plugin: Any
    scope: ServiceScope
    inject: tuple[str, ...] = ()
    pending: bool = False
    unloaded: bool = False
    parent: Optional[str] = None
    children: list[str] = field(default_factory=list)
    #: Set while ``setup`` runs so the host knows a provider appeared and pending
    #: plugins should be reconsidered -- in one pass, not on every load.
    provided: bool = False
    error: Optional[str] = None
    #: Service names this install offered, so unloading it can find its
    #: dependents without a second bookkeeping structure that could drift.
    provided_names: list[str] = field(default_factory=list)
    #: The context this install was activated with.  Kept so a caller can reach it
    #: without reconstructing one -- and so a diagnostic can ask what a plugin was
    #: handed, which is not otherwise recoverable from the install.
    ctx: Any = None


class PluginHost:
    """Owns the plugin tree and the order in which it comes up and goes down.

    The host is deliberately not a service: a plugin must not be able to replace
    the thing that decides what is loaded, or "everything is replaceable" would
    include the replacement mechanism.
    """

    def __init__(
        self,
        services: Optional[ServiceRegistry] = None,
        events: Optional[EventBus] = None,
    ) -> None:
        self.services = services if services is not None else ServiceRegistry()
        self.events = events if events is not None else EventBus()
        #: ``event_type -> Domain | None``, or ``None`` when no domain table is wired.
        #: Set by the composition, which owns the domains; the kernel asks it through
        #: ``Context.on`` so a subscription to another bus's event is refused rather
        #: than silently never called.  See ``agent.core.event_domains.domain_of``.
        self.domain_lookup: Optional[Callable[[type], Any]] = None
        self._installs: dict[str, Install] = {}
        #: Plugins waiting for a dependency, keyed by name.
        self._pending: dict[str, Install] = {}
        #: Names that were loaded and have been unloaded.  Kept so that unloading
        #: twice is harmless -- a plugin may be taken down by its own parent *and*
        #: by an explicit call -- while unloading a name that never existed still
        #: raises, which is the case that indicates a typo.
        self._unloaded: set[str] = set()
        #: Installs whose ``setup`` is running right now, keyed by name.  Everything else
        #: in this class treats "loaded" as "setup has returned", which is wrong *during*
        #: a setup: a plugin that loads a child from its own setup could not be found as
        #: that child's parent, so the link was silently dropped.
        self._activating: dict[str, Install] = {}
        #: Installs that were refused because their ``setup`` raised, keyed by name.
        #: Kept rather than dropped: a plugin that fails to load registers nothing, so
        #: "it is not in ``loaded()``" and "it was never there" looked identical, and a
        #: composition that lost a plugin to an exception could not say so.
        self._failed: dict[str, Install] = {}
        #: Awaitable teardowns claimed from scopes that were disposed by the synchronous
        #: ``unload``, keyed by the plugin that registered them.  Kept rather than dropped:
        #: a scope cannot await, and a teardown that is never awaited is a resource
        #: released nowhere -- so it waits here, by name, until something with an event
        #: loop collects it.  ``pending_async_disposals()`` is how you see it.
        self._async_disposals: dict[str, list[Callable[[], Awaitable[None]]]] = {}

    # -- introspection -------------------------------------------------------

    @property
    def installs(self) -> dict[str, Install]:
        """The loaded installs, **live**.

        Live rather than a copy because a composition manager reads it to answer
        "what is loaded" and must see the current answer; a snapshot would let it
        report a plugin that has since been unloaded.
        """
        return self._installs

    @property
    def pending_installs(self) -> dict[str, Install]:
        """The installs waiting for a dependency, live for the same reason."""
        return self._pending

    def was_unloaded(self, name: str) -> bool:
        """Whether *name* was loaded and has since been unloaded.

        Distinct from "unknown", and the distinction matters to a caller reporting
        a result: unloading twice is harmless (a parent may have taken the plugin
        down already), while unloading a name that never existed is a typo.
        """
        return name in self._unloaded

    def loaded(self) -> tuple[str, ...]:
        """Active plugin names, in activation order."""
        return tuple(self._installs)

    def pending(self) -> tuple[str, ...]:
        """Names waiting for a dependency the composition does not supply."""
        return tuple(self._pending)

    def failed(self) -> dict[str, str]:
        """Plugins whose ``setup`` raised, by name, with the error.

        The third state after loaded and pending, and the one that used to be invisible:
        a refused plugin registers nothing, so it appeared in neither of the other two
        and the only trace was a log line.  A composition can now report it, and a
        caller can assert on it.
        """
        return {name: install.error or "" for name, install in self._failed.items()}

    def failures(self) -> tuple[Install, ...]:
        """The refused installs themselves, for a diagnostic that wants more than a name."""
        return tuple(self._failed.values())

    def waiting_for(self) -> dict[str, tuple[str, ...]]:
        """What each pending plugin is missing.

        Reported rather than logged once: a plugin that never activates registers
        nothing, and "nothing happened" is the hardest kind of silence to debug.
        """
        return {
            name: tuple(d for d in install.inject if not self.services.has(d))
            for name, install in self._pending.items()
        }

    # -- composition ---------------------------------------------------------

    def load(
        self,
        plugin: Any,
        *,
        parent: Optional[str] = None,
        replace: bool = False,
    ) -> Install:
        """Install *plugin*, activating it if its dependencies are present.

        Returns the install record either way: a pending plugin is a known plugin,
        and the caller may want to report what it is waiting for.

        ``replace`` makes replacement a first-class act rather than a collision.  It
        unloads the incumbent first -- which unwinds its registrations *and* takes
        its dependents down -- so a replacement cannot leave the tree holding half of
        one plugin and half of another.  The alternative, silently overwriting the
        name, is what makes "which implementation is live?" depend on load order.

        **And it brings the dependents back.**  Taking them down is right -- they held
        a service whose owner was about to be gone -- but leaving them down is not
        replacement, it is deletion: swapping the provider of ``ltm_store`` used to
        leave ``context_manager`` permanently absent, with no error and nothing
        pending, because the dependent had been *unloaded* rather than left waiting.
        Each one is re-loaded after the replacement is in; if the replacement does not
        supply what it needs, it stays **pending**, which is visible in
        :meth:`waiting_for` rather than silent.
        """
        name = getattr(plugin, "name", None)
        if not isinstance(name, str) or not name:
            raise MissingNameError(
                f"{type(plugin).__name__} has no 'name'; a plugin must be "
                f"identifiable to be looked up, unloaded, or depended on"
            )
        dependents: list[Install] = []
        if name in self._installs or name in self._pending:
            if not replace:
                raise ValueError(f"plugin {name!r} is already loaded")
            dependents = self._capture_dependents(name)
            self.unload(name)

        scope = self.services.scope()
        install = Install(
            name=name,
            plugin=plugin,
            scope=scope,
            inject=tuple(str(d) for d in (getattr(plugin, "inject", ()) or ())),
            parent=parent,
        )
        if parent is not None:
            # ``_installs`` **or** the parent currently running its own setup.  A plugin
            # that loads a child from ``setup`` used to get no link at all, because the
            # loading plugin is not in ``_installs`` until its own ``setup`` returns --
            # measured: a parent's ``children`` was `[]` after `ctx.plugin(Child())`.
            #
            # The link is not what disposes the child: scopes nest, so the child's
            # registrations are unwound by the parent's scope either way.  It is what
            # makes the child *findable* -- for a diagnostic that says which plugin
            # installed what, and for `replace`, whose capture walks this list.
            owner = self._installs.get(parent) or self._activating.get(parent)
            if owner is not None:
                owner.children.append(name)

        if self._activate(install) and install.provided:
            # Only a plugin that just *provided* something can have unblocked a
            # waiter.  Re-checking after every load would activate a plugin the
            # moment any unrelated plugin appeared.
            self._flush_pending()

        for dependent in dependents:
            self.load(dependent.plugin, parent=dependent.parent)
        return install

    def _capture_dependents(self, name: str) -> list[Install]:
        """Everything ``unload(name)`` would take down, in activation order.

        Captured *before* the unload, because afterwards the installs are gone and only
        their names would remain.  Children are included for the same reason
        ``unload`` includes them: a parent's scope owns the records that installed
        them, so they go down too -- and a replacement that restored only the direct
        dependents would leave a subtree missing.

        **Both tables, not only the active one.**  ``unload`` takes down a child whether
        it is active or still pending -- its loop tests ``_installs`` *and* ``_pending``
        -- so a capture that read only ``_installs`` dropped every pending child: it was
        unloaded with its parent and never restored, vanishing from the composition
        entirely.  Measured: a pending child present before a ``replace=True`` was gone
        from both ``loaded()`` and ``pending()`` afterwards.

        Active *dependents* are still the only dependents collected, which is correct
        rather than an omission: ``unload`` does not take pending dependents down (they
        never held the service), so there is nothing to restore.  A pending plugin whose
        provider is replaced simply stays pending, and ``waiting_for`` continues to say
        what it wants.
        """
        taken: set[str] = set()

        def install_for(target: str) -> Install:
            return self._installs.get(target) or self._pending.get(target)

        def collect(target: str) -> None:
            for dependent in self._dependents_of(target):
                if dependent in taken:
                    continue
                taken.add(dependent)
                collect(dependent)
            install = install_for(target)
            for child in list(install.children) if install else []:
                if child in taken:
                    continue
                taken.add(child)
                collect(child)

        collect(name)
        # Activation order, so a provider is re-loaded before the plugin that
        # injects it -- `load` would otherwise leave the dependent pending until a
        # later flush, which is correct but reads as flapping in a log.  Pending
        # ones keep their relative order too; they go back to waiting either way.
        order = [n for n in self._installs if n in taken]
        order += [n for n in self._pending if n in taken]
        return [install_for(n) for n in order]

    def _activate(self, install: Install) -> bool:
        """Run ``setup`` if every injected service exists.  Returns activation."""
        unmet = [d for d in install.inject if not self.services.has(d)]
        if unmet:
            install.pending = True
            self._pending[install.name] = install
            return False

        ctx = self.context_for(install)
        install.ctx = ctx
        # Visible to any child this plugin loads while its own setup runs.  Removed in
        # a `finally` so a setup that raises does not leave the name claimed.
        self._activating[install.name] = install
        setup = getattr(install.plugin, "setup", None)
        if callable(setup):
            try:
                returned = setup(ctx)
            except Exception as exc:
                self._fail(install, exc)
                return False
            finally:
                self._activating.pop(install.name, None)
            if inspect.iscoroutine(returned):
                # An ``async def setup`` returns a coroutine that nothing awaits, so
                # the plugin loads, registers nothing, and reports no error -- the
                # only signal is a RuntimeWarning most deployments suppress.  Refused
                # rather than supported: awaiting here would mean ``load`` is async,
                # which is a change to every caller including the composition root.
                # Closed rather than dropped so the warning is not emitted on a path
                # that has already decided the setup does not count.
                returned.close()
                self._fail(
                    install,
                    TypeError(
                        f"plugin {install.name!r} has an async setup; the kernel calls "
                        "setup synchronously and would register nothing. Make setup "
                        "synchronous, or do the async work in a task it starts. "
                        "For awaitable *teardown*, `astop` and `ctx.on_unwind_async` "
                        "are awaited by `aunload`/`adispose`."
                    ),
                )
                return False

        install.pending = False
        self._activating.pop(install.name, None)
        self._installs[install.name] = install
        self._pending.pop(install.name, None)
        # A name that now works is not a failure any more, and a report that kept saying
        # so would be one somebody learns to ignore.
        self._failed.pop(install.name, None)
        return True

    def _fail(self, install: Install, exc: BaseException) -> None:
        """Refuse an install, unwinding whatever its setup registered first.

        Disposed rather than retried: a setup that raised will raise again, and a retry
        loop is a hang with extra steps.  Both failure modes -- an exception and an
        ``async def`` setup -- go through here, so they cannot leave different states:
        the point of refusing the async form is that its *absence of effect* is the
        problem, and a refusal that half-applied would have the same shape.
        """
        install.scope.dispose()
        install.error = str(exc)
        self._pending.pop(install.name, None)
        self._activating.pop(install.name, None)
        # Recorded, not only logged.  A log line is not part of the API: a composition
        # root that wanted to report "these plugins did not load" had to parse output it
        # does not own, and a caller had no way to ask at all.
        self._failed[install.name] = install
        logger.warning("plugin %r setup failed: %s", install.name, exc)

    def context_for(self, install: Install) -> Any:
        """The context handed to *install*'s ``setup``.

        Imported here rather than at module scope because :mod:`agent.kernel.context`
        needs the host type for its own annotations -- a cycle that a function-local
        import resolves without any of the indirection a forward reference would
        need.
        """
        from agent.kernel.context import Context

        return Context(host=self, install=install)

    def _flush_pending(self) -> None:
        """Activate every pending plugin whose dependencies now exist.

        Iterative rather than recursive: an activated plugin may itself provide
        something a later pending plugin waits for, and that chain must be followed
        without the call stack growing with it.
        """
        progress = True
        while progress:
            progress = False
            for name, install in list(self._pending.items()):
                if any(not self.services.has(d) for d in install.inject):
                    continue
                install.provided = False
                if self._activate(install) and install.provided:
                    progress = True

    # -- teardown ------------------------------------------------------------

    def unload(self, name: str) -> Install:
        """Unload *name* and everything that depends on it.

        Dependents first: a plugin that injected a service this one provides would
        otherwise be left holding a service whose owner is gone.  Unloading a
        dependency therefore unloads whatever declared itself dependent on it --
        which is what the declaration means, not a side effect of it.
        """
        install = (
            self._installs.get(name)
            or self._pending.get(name)
            or self._failed.get(name)
        )
        if install is None:
            if name in self._unloaded:
                # Already gone, most likely because its parent took it down.
                return None  # type: ignore[return-value]
            raise KeyError(f"no such plugin: {name}")
        if install.unloaded:
            return install

        for dependent in self._dependents_of(name):
            self.unload(dependent)

        install.unloaded = True
        install.pending = False
        self._failed.pop(name, None)

        # The children go with the parent, as a consequence of the parent's scope
        # owning the records that installed them.
        for child in list(install.children):
            if child in self._installs or child in self._pending:
                self.unload(child)

        # 1. ``teardown`` -- for what a scope cannot express, while the plugin's own
        #    registrations still exist, because a teardown that cannot see its
        #    resources cannot release them.
        teardown = getattr(install.plugin, "teardown", None)
        if callable(teardown):
            try:
                teardown()
            except Exception as exc:  # noqa: BLE001 - one bad teardown must not strand the rest
                logger.warning("plugin %r teardown failed: %s", name, exc)

        # 1b. ``astop`` -- the awaitable counterpart of ``teardown``, for a plugin that
        #     would rather declare it than register it.  Claimed the same way and waiting
        #     in the same place, so both spellings have one lifetime rule.
        astop = getattr(install.plugin, "astop", None)
        if callable(astop):
            self._async_disposals.setdefault(name, []).append(astop)

        # 2. The scope: every service, every subscription, every child install.
        #
        # Synchronous, and it stays that way.  Whatever needed awaiting is claimed first
        # and kept under this name, so the caller that owns a loop can await it later
        # instead of the handlers being dropped here.
        awaitable = install.scope.take_async_unwind()
        if awaitable:
            self._async_disposals.setdefault(name, []).extend(awaitable)
        install.scope.dispose()

        self._installs.pop(name, None)
        self._pending.pop(name, None)
        self._unloaded.add(name)
        parent = install.parent
        if parent and parent in self._installs:
            try:
                self._installs[parent].children.remove(name)
            except ValueError:
                pass
        return install

    def pending_async_disposals(self) -> tuple[str, ...]:
        """Names whose awaitable teardown has been claimed but not yet run.

        The report that keeps ``on_unwind_async`` honest.  ``unload`` cannot await, so an
        async handler is *claimed* and waits here; a composition that never awaits it would
        otherwise release nothing and say nothing.
        """
        return tuple(name for name, items in self._async_disposals.items() if items)

    async def aunload(self, name: str) -> Install:
        """Unload *name* and await whatever teardown it registered asynchronously."""
        install = self.unload(name)
        await self._await_disposals(name)
        return install

    async def adispose(self) -> None:
        """Await every claimed teardown, for a composition shutting down."""
        for name in list(self._async_disposals):
            await self._await_disposals(name)

    async def _await_disposals(self, name: str) -> None:
        handlers = self._async_disposals.pop(name, [])
        first_error: Optional[BaseException] = None
        for handler in reversed(handlers):
            try:
                await handler()
            except BaseException as exc:  # noqa: BLE001 - one bad teardown must not strand the rest
                logger.warning("plugin %r async teardown failed: %s", name, exc)
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    async def aunload_all(self) -> None:
        """Unload every root, then await the teardowns that needed it."""
        self.unload_all()
        await self.adispose()

    def unload_all(self) -> None:
        """Unload every root, which takes the tree down with it."""
        for name in [n for n, i in self._installs.items() if i.parent is None]:
            if name in self._installs:
                self.unload(name)
        for name in list(self._pending):
            if name in self._pending:
                self.unload(name)
        # And clear the record of what failed: `unload_all` means the composition is
        # empty, and a failure list that outlived it would describe a tree that is gone.
        for name in list(self._failed):
            self.unload(name)

    def _provides(self, name: str) -> set[str]:
        """Service names the install called ``name`` currently provides."""
        install = self._installs.get(name)
        return set(install.provided_names) if install is not None else set()

    def _dependents_of(self, name: str) -> list[str]:
        """Active plugins whose ``inject`` names something this one provides."""
        offered = self._provides(name)
        if not offered:
            return []
        return sorted(
            other
            for other, install in self._installs.items()
            if other != name
            and not install.unloaded
            and offered.intersection(install.inject)
        )

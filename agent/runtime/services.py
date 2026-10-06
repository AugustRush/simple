"""Named services and the disposable scopes that own them.

Why this module exists
----------------------
The repository resolves almost everything by string name out of a dict that
bootstrap fills in: ``ToolRegistry.set_context("subprocess_provider", ...)``
now, ``registry.set_context("components", components)`` for the rest.  That
idiom works, and it is why ``agent/exec/subprocess.py`` can already swap its
execution provider.  What it cannot do is *undo* a registration.  Nothing in
the tree could un-register one name, which is the gap between "configurable"
and "unloadable": a plugin that provides a service has no way to take it back.

So there are two halves here.  ``ServiceRegistry`` is the resolution half,
generalizing what ``provider_from`` already does by hand.  ``ServiceScope`` is
the ownership half, which did not exist.

How a scope unwinds
-------------------
A scope does not own the storage; it records *undo actions* and replays them in
reverse.  That choice is deliberate.  Undoing by "delete everything registered
in this scope" only works when the scope is the sole owner of a private dict,
which is not true of the contexts this has to wrap -- ``ToolRegistry._context``
is a shared dict that forks and the tool-call path both read directly.  An undo
action can restore a previous value, so a scope can override an existing
service and hand the original back on the way out.

The reverse order matters for the same reason it does in a stack: a provider
registered later may have depended on state registered earlier, so it has to be
torn down first.

What this is not
----------------
Not a container with lifetime management, not a dependency graph.  There is no
``inject`` here and nothing waits for a service to appear; a caller that needs
one gets it or gets :class:`MissingServiceError`.  Deferred activation is a much
larger idea and this codebase has not earned it yet.
"""

from __future__ import annotations

from typing import Any, Callable, Iterator, Mapping, Optional

__all__ = [
    "MissingServiceError",
    "ServiceRegistry",
    "ServiceScope",
    "ServiceView",
]


class MissingServiceError(LookupError):
    """Raised when a required service was never provided.

    A distinct type because the alternative -- returning ``None`` -- is how a
    missing dependency turns into an ``AttributeError`` three frames away from
    the thing that was actually absent.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"missing service: {name!r}")
        self.name = name


class ServiceScope:
    """A disposable owner for registrations made through it.

    Enter it with ``with registry.scope():`` or hold it and call
    :meth:`dispose`.  Everything registered while it is open is undone when it
    closes, in reverse order, exactly once.
    """

    __slots__ = ("_registry", "_unwind", "_disposed")

    def __init__(self, registry: "ServiceRegistry") -> None:
        self._registry = registry
        self._unwind: list[Callable[[], None]] = []
        self._disposed = False

    # -- lifetime ---------------------------------------------------------

    @property
    def disposed(self) -> bool:
        return self._disposed

    def __enter__(self) -> "ServiceScope":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.dispose()

    def dispose(self) -> None:
        """Undo this scope's registrations.  Safe to call more than once.

        A handler that raises does not stop the rest: this runs on unload
        paths where one broken teardown must not strand every other
        registration.  The first failure is re-raised after the remainder has
        run, so the caller still learns that something went wrong.
        """
        if self._disposed:
            return
        self._disposed = True
        handlers, self._unwind = self._unwind, []
        first_error: Optional[BaseException] = None
        for handler in reversed(handlers):
            try:
                handler()
            except BaseException as exc:  # noqa: BLE001 - teardown must continue
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    # -- registration -----------------------------------------------------

    def on_unwind(self, handler: Callable[[], None]) -> None:
        """Register a teardown action.

        The escape hatch for what cannot be expressed as a service: closing a
        subprocess handle, releasing a lock, unsubscribing a listener.
        """
        if self._disposed:
            raise RuntimeError("cannot register on a disposed scope")
        self._unwind.append(handler)

    def provide(self, name: str, value: Any, *, replace: bool = False) -> None:
        """Provide *name* for the lifetime of this scope.

        The previous state is captured *before* the write, so the recorded undo
        restores what this scope replaced rather than the value it installed.
        """
        if self._disposed:
            raise RuntimeError("cannot provide on a disposed scope")
        restore = self._registry._capture_previous(name)
        self._registry.provide(name, value, replace=replace)
        self._unwind.append(restore)


class ServiceRegistry:
    """A flat name -> service map with disposable registration scopes.

    Cheap on purpose: most callers want a dictionary with an owner, and the
    ones that need a dependency graph are not in this codebase yet.
    """

    __slots__ = ("_services", "_open_scopes")

    def __init__(self, initial: Optional[Mapping[str, Any]] = None) -> None:
        self._services: dict[str, Any] = dict(initial) if initial else {}
        self._open_scopes: list[ServiceScope] = []

    @classmethod
    def adopt(cls, storage: dict[str, Any]) -> "ServiceRegistry":
        """Wrap a dict the caller already owns, instead of copying it.

        The runtime builds its component mapping incrementally and assigns into
        it directly, then hands it to :class:`RuntimeComponents`.  Copying here
        would silently split the two: a later assignment to the mapping would be
        invisible to the registry and vice versa.  Adopting makes the mapping and
        the container the same storage, which is what lets the dict-based call
        sites and ``require()`` agree during the migration.
        """
        registry = cls()
        registry._services = storage
        return registry

    # -- resolution -------------------------------------------------------

    _MISSING = object()

    def get(self, name: str, default: Any = _MISSING) -> Any:
        """Resolve *name*, or *default*, or raise :class:`MissingServiceError`."""
        try:
            return self._services[name]
        except KeyError:
            if default is not ServiceRegistry._MISSING:
                return default
            raise MissingServiceError(name) from None

    def has(self, name: str) -> bool:
        return name in self._services

    def names(self) -> tuple[str, ...]:
        return tuple(self._services)

    def as_dict(self) -> dict[str, Any]:
        """A snapshot copy.  Mutating it does not touch the registry."""
        return dict(self._services)

    # -- registration -----------------------------------------------------

    def provide(self, name: str, value: Any, *, replace: bool = False) -> None:
        """Provide *name*.

        Refuses to overwrite unless *replace* is set: two providers claiming
        one name is a configuration error, and the second one winning silently
        is how a mismatch survives to production.
        """
        if name in self._services and not replace:
            raise ValueError(
                f"service {name!r} is already provided; "
                "pass replace=True to overwrite it"
            )
        self._services[name] = value

    def _capture_previous(self, name: str) -> Callable[[], None]:
        """Build the reverse of a :meth:`provide` call.

        Must be called *before* the write it undoes.  What it records is the
        state this registration is about to replace: a value to put back, or
        the fact that nothing was there.  Capturing after the write would
        record the newly installed value and make the undo a no-op.
        """
        had_value = name in self._services
        previous = self._services.get(name)

        def restore() -> None:
            if had_value:
                self._services[name] = previous
            else:
                self._services.pop(name, None)

        return restore

    # -- scopes -----------------------------------------------------------

    def scope(self) -> ServiceScope:
        """Open a scope whose registrations unwind together."""
        scope = ServiceScope(self)
        self._open_scopes.append(scope)
        return scope

    def dispose_scopes(self) -> None:
        """Dispose every scope still open.  For teardown of the whole registry."""
        for scope in list(self._open_scopes):
            scope.dispose()
        self._open_scopes.clear()


class ServiceView(Mapping[str, Any]):
    """A read-only dict-like view over a :class:`ServiceRegistry`.

    Existing code reads service state with ``get_context(key, default)`` on a
    plain dict.  This lets that code keep its shape while the storage moves to
    a registry, so the migration does not have to be all at once.
    """

    __slots__ = ("_registry",)

    def __init__(self, registry: ServiceRegistry) -> None:
        self._registry = registry

    def __getitem__(self, key: str) -> Any:
        return self._registry.get(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._registry.names())

    def __len__(self) -> int:
        return len(self._registry.names())

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-compatible ``get`` with a ``None`` default, unlike the registry's."""
        return self._registry.get(key, default)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self._registry.has(key)

    @property
    def registry(self) -> ServiceRegistry:
        return self._registry

"""Typed events: the kernel's second extension point after services.

The type is the channel
-----------------------
There is no string bus and no ``emit("whatever", data)``.  A listener subscribes to
one event *class*, so a handler for ``ToolPreExecute`` cannot be called for an
unrelated event by a typo in a name -- and a new domain is a new class, not a new
string that has to be agreed on in two places.

Two delivery modes, and only the domains that need blocking get one
-------------------------------------------------------------------
``emit`` notifies: every listener runs, failures are isolated, and the first
non-``None`` result is returned for a caller that wants it.

``emit_blocking`` stops at the first listener that returns a truthy value.  That
short-circuit is what makes a gate a gate -- a listener that can be argued with by
a later listener is not one -- and it is *not* the default because a domain that
inherited it by accident would silently stop calling every listener behind the
first truthy return, with no local symptom.  Which domains may block is recorded
in ``agent/core/event_domains.py``.

Isolation is the contract
-------------------------
A listener that raises is reported and skipped.  Mounting a plugin must not be able
to break the thing it observes, and this is on the hot path of every turn.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

__all__ = ["EventBus", "Listener"]


Listener = Callable[[Any], Any]


class EventBus:
    """In-process typed event delivery.

    Listeners are matched by exact event class, delivered in registration order,
    and awaited when they are coroutine functions.
    """

    __slots__ = ("_listeners", "_on_error")

    def __init__(self, on_error: Callable[[Any, BaseException], None] | None = None) -> None:
        self._listeners: dict[type, list[Listener]] = {}
        #: Where a swallowed listener failure is reported.  Injectable so the bus
        #: does not have to import the console, and so a test can assert the report
        #: instead of reading it off stdout.
        self._on_error = on_error

    # -- subscription --------------------------------------------------------

    def on(self, event_type: type, listener: Listener) -> Callable[[], None]:
        """Subscribe *listener* to *event_type*; returns an unsubscribe callable.

        The callable is what makes a subscription an *effect*: a scope records it
        and reversing the scope removes the listener, so a plugin that unloads
        cannot leave one behind.  A listener left on a bus after its plugin is gone
        is called for ever and raises into the isolation handler every time.
        """
        self._listeners.setdefault(event_type, []).append(listener)

        def unsubscribe() -> None:
            listeners = self._listeners.get(event_type)
            if not listeners:
                return
            try:
                listeners.remove(listener)
            except ValueError:
                pass
            if not listeners:
                self._listeners.pop(event_type, None)

        return unsubscribe

    def has_listeners(self, event_type: type) -> bool:
        return bool(self._listeners.get(event_type))

    def listeners(self, event_type: type) -> tuple[Listener, ...]:
        return tuple(self._listeners.get(event_type, ()))

    # -- delivery ------------------------------------------------------------

    async def emit(self, event: Any) -> Any:
        """Notify every listener; return the first non-``None`` result, if any."""
        result: Any = None
        for listener in self.listeners(type(event)):
            value = await self._invoke(listener, event)
            if value is not None and result is None:
                result = value
        return result

    async def emit_blocking(self, event: Any) -> Any:
        """Notify listeners until one refuses, returning its reason.

        ``None`` (or any falsy value) means "no opinion", and the next listener is
        asked.  A truthy value stops delivery and is returned as the reason.
        """
        for listener in self.listeners(type(event)):
            value = await self._invoke(listener, event)
            if value:
                return value
        return None

    async def _invoke(self, listener: Listener, event: Any) -> Any:
        try:
            if inspect.iscoroutinefunction(listener):
                return await listener(event)
            return listener(event)
        except Exception as exc:  # noqa: BLE001 - isolation is the contract
            if self._on_error is not None:
                self._on_error(event, exc)
            return None

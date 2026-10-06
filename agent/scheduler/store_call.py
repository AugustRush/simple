"""How the scheduler gets a store it can call from a worker thread.

The problem
-----------
``SchedulerService._store_call`` ran every store method off-loop through
``asyncio.to_thread``.  That is right for a store that owns one connection, but
wrong for the SQLite store: a connection cannot be used from another thread, so
when the store *was* a ``SchedulerStore`` the service opened a fresh
thread-owned connection for each call instead.

It expressed that as ``type(self.store) is not SchedulerStore`` -- a concrete
class test inside the scheduler.  The trade-off is real, but the *knowledge*
belongs with the store that has it: only the SQLite store knows that its
connection is thread-bound.

The fix is one method
---------------------
``_calling_in_thread()`` returns the object the scheduler should call from
inside the worker thread.  ``SchedulerStore`` returns a new store opened on the
same database, which owns its own connection; every other store returns itself.
The scheduler then has no branch and no class test -- it asks the store how it
wants to be called.

The distinction that keeps this safe
------------------------------------
Identity, not equality.  ``type(self) is SchedulerStore`` fires only for the
class itself, never for a subclass, so a `SchedulerStore` subclass keeps getting
its parent's connection -- exactly what happened before, bug included.  That is
deliberate for a change with no behavioural intent: the point is to move *where*
the decision lives, not to alter *what* it decides.
"""

from __future__ import annotations

from typing import Any, Callable

__all__ = ["call_store_method"]


async def call_store_method(
    store: Any,
    method_name: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Call one store method off the event loop, on the store's own terms.

    ``_calling_in_thread`` is asked for the object to call; a store that returns
    itself is called directly in the worker thread.  The call is shielded so a
    cancellation does not leave a thread committing after the caller has been
    told the call was abandoned -- a thread cannot be cancelled, so it is waited
    for instead.
    """
    import asyncio

    worker_store = _thread_store_for(store)

    def invoke() -> Any:
        return getattr(worker_store, method_name)(*args, **kwargs)

    operation = asyncio.create_task(asyncio.to_thread(invoke))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        import contextlib

        with contextlib.suppress(Exception):
            await operation
        raise


def _thread_store_for(store: Any) -> Any:
    """The object to call from a worker thread: the store's own answer.

    Falls back to the store itself when it does not implement
    ``_calling_in_thread``, so a duck-typed double in a test -- or any store
    written before this seam existed -- still works.
    """
    hook: Callable[[], Any] | None = getattr(store, "_calling_in_thread", None)
    if hook is None:
        return store
    return hook()

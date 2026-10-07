"""The kernel: the smallest thing that lets plugins be composed.

Its whole job is the sentence in the plan: *let plugins be composed, depend on
each other, provide services, listen to events, and be unloaded or replaced.*
Nothing here knows what an agent, a model, a tool or a session is.

The six concepts, and nothing else
----------------------------------
``Context``
    What a plugin is handed.  ``provide`` / ``get`` / ``on`` / ``emit`` /
    ``scope`` / ``plugin`` -- and no other vocabulary.
``Plugin``
    A thing with a ``setup(ctx)``, an optional ``teardown()``, and a declared
    ``inject`` list.  A plugin is identified by its ``name``.
``Service``
    A named object in the registry, provided for the lifetime of a scope and
    removed when that scope is disposed.
``Event``
    A typed emission.  The type *is* the channel; there is no string bus.
``Scope``
    The unit of reversal.  Every registration a plugin makes belongs to one, so
    disposing it undoes all of them -- registrations are effects.
``Lifecycle``
    Install, wait for dependencies, activate, deactivate, dispose.

What is deliberately absent
---------------------------
No privileged participants.  There is no ``Agent``, ``Model``, ``Tool`` or
``Session`` type in here, because in this design those are plugins like any
other -- the kernel would have to know their names to privilege them, and the
point is that it does not.

No framework, either.  The plan is explicit that a personal agent written in
Python should not grow a plugin framework larger than the agent: this is six
concepts in a few hundred lines, not a reimplementation of Cordis.  What is
borrowed is the *mechanism* (reversible effects, injected services, typed
events, scopes) rather than the API surface.
"""

from __future__ import annotations

from agent.kernel.service import (
    MissingServiceError,
    ServiceRegistry,
    ServiceScope,
    ServiceView,
    require_service,
    resolve,
)

__all__ = [
    "MissingServiceError",
    "ServiceRegistry",
    "ServiceScope",
    "ServiceView",
    "require_service",
    "resolve",
]

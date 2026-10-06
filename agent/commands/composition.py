"""Constructing the command router: one place, not four.

``components["command_router"]`` was written from four sites -- the bootstrap,
the CLI, the base channel and the web channel -- and every one of them built the
*wire* the same way:

    router = CommandRouter(skill_catalog=...)
    register_builtin_commands(router)
    router.register_plugin_catalog(plugin_catalog)
    components["command_router"] = router

Four copies of one construction is the same debt as the twenty-two
``SchedulerStore`` constructions the scheduler had, and it has the same
consequence: "which router does this process use?" is answered by *whoever ran
last*, not by a single decision.  The three non-bootstrap copies existed because
bootstrap's router was not always reachable from where the channel or CLI was
constructed, and a local rebuild was the available workaround.

``ensure_command_router`` is that single decision, with the caching each call
site was doing by hand kept in one place.

Why the router is not merely provided by bootstrap
--------------------------------------------------
Because it cannot always be: the CLI and the channels are constructed in
compositions where bootstrap's dict is not in hand, which is precisely why three
copies appeared.  A single *function* that takes the mapping works everywhere the
four copies did, and does not require every composition to go through bootstrap
first.  Making the bootstrap the only writer would have moved the problem rather
than removed it.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["ensure_command_router"]

#: The key all four former sites agreed on, named once.
ROUTER_KEY = "command_router"


def ensure_command_router(
    components: MutableMapping[str, Any] | Mapping[str, Any],
) -> Any:
    """The command router for *components*, building and caching it if needed.

    Idempotent: the second call returns the first call's router rather than
    registering the builtin commands onto a fresh one.  That matters because
    ``register_builtin_commands`` is additive -- building twice would leave two
    routers with divergent plugin registrations, and which one a request used
    would depend on the entry point it arrived through.

    *components* is expected to be the mutable component mapping.  Caching is
    attempted rather than tested for: a caller may hold a mapping-like object
    that supports ``__setitem__`` without registering as a
    :class:`~collections.abc.MutableMapping`, and an ``isinstance`` check
    silently skipped the cache for exactly such an object -- the web channel's
    component holder, which then built a fresh router on every request and
    returned an empty command list.  A failed write is the honest test of
    whether a write is possible.
    """
    from agent.commands import CommandRouter, register_builtin_commands

    cached = components.get(ROUTER_KEY)
    if cached is not None:
        return cached

    router = CommandRouter(skill_catalog=components.get("skill_catalog"))
    register_builtin_commands(router)

    plugin_catalog = components.get("plugin_catalog")
    if plugin_catalog is not None and hasattr(plugin_catalog, "get_slash_commands"):
        router.register_plugin_catalog(plugin_catalog)

    try:
        components[ROUTER_KEY] = router  # type: ignore[index]
    except TypeError:
        # A read-only mapping: a caller that does not want the cache, not an
        # error.  The router is still returned so the call site works.
        pass
    return router

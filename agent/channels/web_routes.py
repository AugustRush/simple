"""HTTP endpoints a plugin contributes, as a capability service.

Why this exists
---------------
Every other kind of contribution in this tree is a capability service -- tools,
skills, channels, commands, agents, delivery targets, system jobs -- and the web
surface was the one that was not. `agent/channels/web.py` holds its routes as a
literal list, so a plugin that wanted to answer a request had to edit that file.
Measured on the plugin question that started this: a stock-screener plugin could
be a tool, a skill, a scheduled job or a self-contained HTML report, and could
not be a page with an endpoint of its own.

What a contributed route is
---------------------------
    async def handler(request) -> Response

The same signature Starlette gives the built-ins, so a plugin author needs no
new vocabulary. It is a closure over whatever the plugin captured in `setup`,
which is how a handler reaches its own services without this module handing out
the composition.

Two decisions worth stating
---------------------------
**Authorized by default.** Every contributed route is wrapped in the channel's
own `_authorized` check before it is registered. A plugin that forgets to check
the auth token would otherwise publish an unauthenticated endpoint on a gateway
the operator may have bound to a LAN address -- and the operator would have no
reason to suspect it. A route that genuinely wants to be public can say so, in
one word, in one place, which is a decision somebody made rather than one they
forgot.

**Mounted after the built-ins.** A contributed path cannot shadow `/api/home` or
`/api/sessions`. Predictability beats flexibility here: the app's own endpoints
are the contract the frontend is written against, and a plugin that could replace
one would make every frontend bug a question of which plugin was loaded.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from agent.kernel.service import ScopedContribution

__all__ = [
    "WebRoute",
    "WebRouteRegistry",
    "ScopedWebRoutes",
    "shared_registry",
]

#: ``async def handler(request) -> Response``
WebRoute = Callable[..., Awaitable[Any]]


class WebRouteRegistry:
    """Which HTTP endpoints plugins contribute."""

    def __init__(self) -> None:
        #: ``(method, path) -> handler``. Keyed by both so two plugins may serve
        #: different methods on one path, which is what a REST-shaped
        #: contribution looks like.
        self._routes: dict[tuple[str, str], WebRoute] = {}
        self._owners: dict[tuple[str, str], str] = {}
        #: Routes that asked not to be wrapped in the auth check. Empty in
        #: practice; the flag exists so "public" is a written decision rather than
        #: a missing one.
        self._public: dict[tuple[str, str], bool] = {}
        #: Owners whose undo has run. An exact liveness signal: a plugin's scope
        #: unwinds its contributions in order, so "my undo ran" means "I am
        #: going away". The first attempt asked whether the owner still had other
        #: routes -- which is wrong for a plugin whose only route was replaced.
        self._gone: set[str] = set()

    def register(
        self,
        method: str,
        path: str,
        handler: WebRoute,
        *,
        owner: str = "",
        public: bool = False,
        replace: bool = False,
    ) -> Callable[[], None]:
        """Add one endpoint. Returns the undo that removes exactly it."""
        verb = str(method or "").strip().upper()
        clean_path = str(path or "").strip()
        if verb not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError(f"not an HTTP method: {method!r}")
        if not clean_path.startswith("/"):
            raise ValueError(
                f"a contributed route needs an absolute path: {path!r}"
            )
        if not clean_path.startswith("/api/plugin/"):
            # Not a style rule: a namespace is what lets the app promise that its
            # own endpoints mean what the frontend thinks they mean.
            raise ValueError(
                "contributed routes live under /api/plugin/: "
                f"{clean_path!r} would sit in the app's own namespace"
            )
        if not isinstance(public, bool):
            # `bool("false")` is True. A plugin reading `public` from JSON config
            # would have published a route it meant to keep private.
            raise ValueError(
                f"route {verb} {clean_path}: public must be True or False, "
                f"not {public!r}"
            )
        try:
            from starlette.routing import compile_path

            compile_path(clean_path)
        except Exception as exc:  # noqa: BLE001 - the reason is the message
            # Checked here rather than where the Route is built: a typo in a
            # plugin's path used to surface inside `bind_runtime`, where it takes
            # the whole web channel down instead of failing that one plugin.
            raise ValueError(
                f"route {verb} {clean_path} is not a valid path: {exc}"
            ) from None
        if not callable(handler):
            raise ValueError(f"route {verb} {clean_path} needs a callable handler")
        key = (verb, clean_path)
        if key in self._routes and not replace:
            existing = self._owners.get(key) or "a built-in"
            raise ValueError(
                f"{verb} {clean_path} is already provided by {existing}"
            )
        previous = self._routes.get(key)
        previous_owner = self._owners.get(key, "")
        # The public flag is part of what a replacement replaces. Captured and
        # restored with the handler: without this, plugin B replacing plugin A's
        # *private* route with a public one and then unloading left A's handler
        # served with `public=True` -- an endpoint A never published, reachable
        # without the auth check. Measured with a two-registry repro.
        previous_public = self._public.get(key, False)
        self._routes[key] = handler
        self._owners[key] = str(owner or "")
        if owner:
            # A plugin that registers again is loaded again.
            self._gone.discard(str(owner))
        self._public[key] = bool(public)

        def undo() -> None:
            # Recorded first: A's undo runs while B still owns the key, so it
            # returns at the identity check below -- but A is still going away.
            if owner:
                self._gone.add(owner)
            if self._routes.get(key) is not handler:
                return
            # Restoring the previous handler only makes sense while its plugin is
            # still loaded. Measured: A registers, B replaces it, A is unloaded
            # first and B second -- B's undo then put A's handler back, and an
            # endpoint belonging to an unloaded plugin kept answering (publicly,
            # if A had said so).
            restore = previous is not None and previous_owner not in self._gone
            if restore:
                self._routes[key] = previous
                self._owners[key] = previous_owner
                self._public[key] = previous_public
            else:
                self._routes.pop(key, None)
                self._owners.pop(key, None)
                self._public.pop(key, None)

        return undo

    def unregister(self, method: str, path: str) -> None:
        """Drop one endpoint, whatever registered it."""
        key = (str(method or "").strip().upper(), str(path or "").strip())
        self._routes.pop(key, None)
        self._owners.pop(key, None)
        self._public.pop(key, None)

    def routes(self) -> list[tuple[str, str, WebRoute, bool]]:
        """Every contributed route as ``(method, path, handler, public)``."""
        return [
            (verb, path, self._routes[(verb, path)], self._public.get((verb, path), False))
            for verb, path in sorted(self._routes)
        ]

    def scoped(self, ctx: Any) -> "ScopedWebRoutes":
        return ScopedWebRoutes(self, ctx)


class ScopedWebRoutes(ScopedContribution):
    """One plugin's view: attribution plus an undo that unwinds with it."""

    def add(
        self,
        method: str,
        path: str,
        handler: WebRoute,
        *,
        public: bool = False,
        replace: bool = False,
    ) -> None:
        self._contribute(
            self._registry.register(
                method,
                path,
                handler,
                owner=self.owner,
                public=public,
                replace=replace,
            )
        )

    def paths(self) -> list[str]:
        return [path for _verb, path, _handler, _public in self._registry.routes()]


#: The registry a composition provides. Module-level for the same reason the
#: delivery-target one is: the channel that mounts these is built by the
#: composition, and a second registry would be a second answer to "which
#: endpoints did plugins add".
_SHARED = WebRouteRegistry()


def shared_registry() -> WebRouteRegistry:
    return _SHARED

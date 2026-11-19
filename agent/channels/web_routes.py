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
            # own endpoints mean what the frontend thinks they mean. `/api/plugin/`
            # is the one prefix no built-in route uses.
            raise ValueError(
                "contributed routes live under /api/plugin/: "
                f"{clean_path!r} would sit in the app's own namespace"
            )
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
        self._routes[key] = handler
        self._owners[key] = str(owner or "")
        self._public[key] = bool(public)

        def undo() -> None:
            if self._routes.get(key) is not handler:
                return
            if previous is not None:
                self._routes[key] = previous
                self._owners[key] = previous_owner
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

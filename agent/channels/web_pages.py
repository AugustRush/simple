"""Whole pages a plugin contributes, shown inside a sandboxed frame.

Why the frame
-------------
Two ways to let a plugin draw a page: render its declarative schema with our own
components, or host its own HTML in a frame. The first makes safety a property of
every field mapping we write -- one that lets a `javascript:` href through is a
cross-site script, and nothing about the schema prevents it. The second makes
safety a property of the browser: `sandbox="allow-scripts"` without
`allow-same-origin` puts the page in an opaque origin, and it cannot read the
app's cookies, storage or DOM. Measured against what a plugin author has to learn,
the frame is also the shorter conversation: "give me an HTML page".

The cost is accepted and written down: a plugin's page will not look like ours.
So the *shell* around it is ours -- page heading, padding, background -- and the
frame's interior is the plugin's world.

What this module is not
-----------------------
Not a settings-section contributor, not a two-way channel, not a way for a plugin
to put things inside 对话/看板/设置. Those are three different features with three
different trust arguments, and none of them is needed to answer "let my plugin
show a page".
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from agent.kernel.service import ScopedContribution

__all__ = [
    "PageRenderer",
    "WebPageRegistry",
    "ScopedWebPages",
    "shared_registry",
    "PAGE_ROUTE_PREFIX",
]

#: Where pages are served. One prefix, one shape: a page is not a REST resource,
#: so this is GET-only and the id is a name rather than a path.
PAGE_ROUTE_PREFIX = "/api/pages/"

#: ``str`` for a static page, or ``(request) -> str`` for one built per request.
PageRenderer = Any


class WebPageRegistry:
    """Which pages plugins contribute."""

    def __init__(self) -> None:
        self._pages: dict[str, dict[str, Any]] = {}
        self._owners: dict[str, str] = {}
        #: Owners whose undo has run -- an exact "this plugin is going away"
        #: signal, for the same reason the route registry keeps one.
        self._gone: set[str] = set()

    def register(
        self,
        page_id: str,
        *,
        title: str,
        html: PageRenderer,
        description: str = "",
        owner: str = "",
        replace: bool = False,
    ) -> Callable[[], None]:
        """Add one page. Returns the undo that removes exactly it."""
        clean = str(page_id or "").strip()
        if not clean:
            raise ValueError("a contributed page needs an id")
        if not clean.replace("-", "").replace("_", "").isalnum():
            # The id is a URL segment and a hash route; anything else needs
            # encoding we would have to agree on with the client.
            raise ValueError(
                f"page id must be letters, digits, '-' or '_': {page_id!r}"
            )
        if not str(title or "").strip():
            raise ValueError(f"page {clean!r} needs a title to show in the app")
        if not (isinstance(html, str) or callable(html)):
            raise ValueError(f"page {clean!r} needs html as a string or a callable")
        if clean in self._pages and not replace:
            existing = self._owners.get(clean) or "a built-in"
            raise ValueError(f"page {clean!r} is already provided by {existing}")
        previous = self._pages.get(clean)
        previous_owner = self._owners.get(clean, "")
        self._pages[clean] = {
            "id": clean,
            "title": str(title).strip(),
            "description": str(description or "").strip(),
            "html": html,
        }
        self._owners[clean] = str(owner or "")
        if owner:
            self._gone.discard(str(owner))

        def undo() -> None:
            if owner:
                self._gone.add(owner)
            if self._pages.get(clean, {}).get("html") is not html:
                return
            # Same rule as the route registry: a previous page is only put back
            # while the plugin that declared it is still here.
            restore = previous is not None and previous_owner not in self._gone
            if restore:
                self._pages[clean] = previous
                self._owners[clean] = previous_owner
            else:
                self._pages.pop(clean, None)
                self._owners.pop(clean, None)

        return undo

    def unregister(self, page_id: str) -> None:
        clean = str(page_id or "").strip()
        self._pages.pop(clean, None)
        self._owners.pop(clean, None)

    def page(self, page_id: str) -> Optional[dict[str, Any]]:
        return self._pages.get(str(page_id or "").strip())

    def listing(self) -> list[dict[str, Any]]:
        """Every page, without its HTML -- the navigation needs names, not bodies."""
        return [
            {
                "id": entry["id"],
                "title": entry["title"],
                "description": entry["description"],
            }
            for _key, entry in sorted(self._pages.items())
        ]

    def scoped(self, ctx: Any) -> "ScopedWebPages":
        return ScopedWebPages(self, ctx)


class ScopedWebPages(ScopedContribution):
    """One plugin's view: attribution plus an undo that unwinds with it."""

    def add(
        self,
        page_id: str,
        *,
        title: str,
        html: PageRenderer,
        description: str = "",
        replace: bool = False,
    ) -> None:
        self._contribute(
            self._registry.register(
                page_id,
                title=title,
                html=html,
                description=description,
                owner=self.owner,
                replace=replace,
            )
        )

    def pages(self) -> list[str]:
        return [entry["id"] for entry in self._registry.listing()]


#: The registry a composition provides. Module-level for the same reason the
#: delivery-target and web-route registries are: a second one would be a second
#: answer to "which pages did plugins add".
_SHARED = WebPageRegistry()


def shared_registry() -> WebPageRegistry:
    return _SHARED

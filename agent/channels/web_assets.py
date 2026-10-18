"""Which frontend bundle the gateway serves, and the route that hands it out.

Both decisions here are about files on disk rather than about the HTTP surface, which is why
they can be read apart from the routes: the bundled ``agent/_builtin/web/dist`` is the canonical
runtime asset, ``frontend/dist`` is a local build that may be stale and is only served when
``SIMPLE_WEB_USE_SOURCE_DIST`` says so, and the index route falls back to a plain API notice
when neither has an ``index.html``.

Extracted from ``agent/channels/web.py`` (see the remaining-big-files survey).  ``_web_dist_dir``
resolves both paths from ``__file__``, so it stayed at the same package depth on purpose:
``parent.parent`` and ``parents[2]`` still mean ``agent/`` and the repository root.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

__all__ = [
    "_web_dist_dir",
    "_index",
]


def _web_dist_dir() -> Path:
    """Resolve the frontend bundle used by the gateway.

    The bundled copy is the canonical runtime asset and is checked into
    the package.  ``frontend/dist`` is intentionally ignored by git and
    may contain an older local build, so it must never silently override
    the bundled assets after a restart.  Set ``SIMPLE_WEB_USE_SOURCE_DIST``
    to opt into source-dist serving during local frontend development.
    """
    package_dist = Path(__file__).resolve().parent.parent / "_builtin" / "web" / "dist"
    source_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    # Vite can leave ``index.html`` behind when a build is interrupted or
    # when only the source tree has been copied.  Treat that as an
    # incomplete bundle; StaticFiles raises during app construction if the
    # referenced assets directory is missing.  Falling back to the bundled
    # release keeps the gateway usable until the next frontend build.
    use_source = os.getenv("SIMPLE_WEB_USE_SOURCE_DIST", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if use_source and (source_dist / "index.html").is_file() and (source_dist / "assets").is_dir():
        return source_dist
    return package_dist


async def _index(
    request: Any,
) -> Any:
    from starlette.responses import HTMLResponse

    dist = _web_dist_dir() / "index.html"
    if dist.is_file():
        return _uncached(HTMLResponse(dist.read_text(encoding="utf-8")))
    return _uncached(HTMLResponse(
        "<html><body style='font-family:sans-serif;padding:40px'>"
        "<h1>Simple Agent API</h1>"
        "<p>This is the API endpoint for web integrations.</p>"
        "</body></html>"
    ))


def _uncached(response: Any) -> Any:
    """Mark the entry document as revalidate-on-every-load.

    The document is the one file that names the bundle (``index-<hash>.js``).
    Cached, it keeps pointing at a bundle that a newer build has already
    deleted -- and because the old script is cached too, the page keeps working
    from cache and simply *never* shows the new UI.  That is the shape of "I
    rebuilt the frontend and nothing changed": the tab was serving last week's
    entry document, not the new one.

    ``no-cache`` rather than ``no-store``: the document may be stored, it just
    has to be revalidated, and it is 411 bytes -- there is nothing to save by
    trusting a copy.  The hashed assets themselves keep their ETag and may be
    cached forever: their name changes when their content does.
    """
    response.headers["Cache-Control"] = "no-cache"
    return response

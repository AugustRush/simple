"""Provides ``web_routes``: the HTTP endpoints plugins contribute.

Nothing is injected -- a registry has no dependencies, which is what lets a
plugin add a route before the channel that mounts it is started. A contributor
whose ``inject`` named the channel would be pending until the channel existed,
and the channel builds its route table from this registry.
"""

from __future__ import annotations

from typing import Any

__all__ = ["WebRoutesPlugin"]


class WebRoutesPlugin:
    """Provides ``web_routes``."""

    name = "web_routes"

    inject = ()

    def setup(self, ctx: Any) -> None:
        from agent.channels.web_routes import shared_registry

        ctx.provide("web_routes", shared_registry())

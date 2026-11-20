"""Provides ``web_pages``: the whole pages plugins contribute.

Nothing is injected -- a registry has no dependencies, which is what lets a
plugin declare a page before the channel that serves it exists.
"""

from __future__ import annotations

from typing import Any

__all__ = ["WebPagesPlugin"]


class WebPagesPlugin:
    """Provides ``web_pages``."""

    name = "web_pages"

    inject = ()

    def setup(self, ctx: Any) -> None:
        from agent.channels.web_pages import WebPageRegistry

        # One registry per composition, not the process-wide fallback -- the same
        # rule `DeliveryTargetsPlugin` follows, for the same measured reason: two
        # compositions in one process (a test suite is the obvious one, and
        # `bootstrap.channels_only()` builds a second PluginHost) would share one
        # registry, so a route or page contributed in one would appear in the
        # other's channel.
        ctx.provide("web_pages", WebPageRegistry())

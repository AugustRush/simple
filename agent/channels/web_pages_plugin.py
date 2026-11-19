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
        from agent.channels.web_pages import shared_registry

        ctx.provide("web_pages", shared_registry())

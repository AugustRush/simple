"""The channel layer's service protocols.

``channels`` is the capability service a channel plugin contributes to.  Its interface
has two halves, and they are deliberately two protocols:

* :class:`ChannelRegistryService` -- what the *composition* does with the registry:
  read what was contributed (``all``) and hand a plugin its view (``scoped``);
* :class:`ChannelContributionView` -- what a *plugin* does with it: ``add`` one
  channel, which the view attributes to the plugin and undoes when it unloads.

Writing the second one down matters more than it looks: it is the whole contract of a
channel plugin, and it is why ``WebChannelPlugin``/``FeishuChannelPlugin`` hold nothing
but a section of config and one call.  A replacement registry whose view lacked ``add``
would break every channel plugin while still satisfying ``all`` and ``scoped``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["ChannelContributionView", "ChannelRegistryService"]


@runtime_checkable
class ChannelContributionView(Protocol):
    """One plugin's handle on the channel registry."""

    def add(self, channel: Any) -> Any:
        """Contribute *channel*; it is removed when the contributing plugin unloads.

        Attribution and the undo belong to the view: a plugin supplies the transport
        and nothing else, which is what keeps "who contributed this channel" from
        being a second thing every plugin has to remember.
        """


@runtime_checkable
class ChannelRegistryService(Protocol):
    """The channel registry a composition provides as ``channels``."""

    def all(self) -> "tuple[Any, ...]":
        """Every contributed channel, in contribution order -- what a gateway runs."""

    def scoped(self, ctx: Any) -> ChannelContributionView:
        """The view for one plugin (the kernel calls this by name for ``ctx.channels``)."""


@runtime_checkable
class WebRouteService(Protocol):
    """What a provider of ``web_routes`` has to offer.

    Three members and the view factory, each with a caller: ``routes`` is the
    channel building its route table, ``register``/``unregister`` are the
    contribution and its undo, and ``scoped`` is how a plugin reaches this as
    ``ctx.web_routes``.
    """

    def register(
        self,
        method: str,
        path: str,
        handler: Any,
        *,
        owner: str = "",
        public: bool = False,
        replace: bool = False,
    ) -> Any:
        """Add one endpoint; returns the undo that removes or restores it."""

    def unregister(self, method: str, path: str) -> None:
        """Drop one endpoint, whatever registered it."""

    def routes(self) -> Any:
        """Every contributed route as ``(method, path, handler, public)``."""

    def scoped(self, ctx: Any) -> Any:
        """A per-plugin view: ``add`` is attributed and unwinds with the plugin."""


@runtime_checkable
class WebPageService(Protocol):
    """What a provider of ``web_pages`` has to offer.

    ``page`` and ``listing`` are the server's lookup and the navigation's list;
    ``register``/``unregister`` are the contribution and its undo; ``scoped`` is
    how a plugin reaches this as ``ctx.web_pages``.
    """

    def register(
        self,
        page_id: str,
        *,
        title: str,
        html: Any,
        description: str = "",
        owner: str = "",
        replace: bool = False,
    ) -> Any:
        """Add one page; returns the undo that removes or restores it."""

    def unregister(self, page_id: str) -> None:
        """Drop one page, whatever registered it."""

    def page(self, page_id: str) -> Any:
        """One page's entry, or ``None``."""

    def listing(self) -> Any:
        """Every page's id, title and description -- never its HTML."""

    def scoped(self, ctx: Any) -> Any:
        """A per-plugin view: ``add`` is attributed and unwinds with the plugin."""

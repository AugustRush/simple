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

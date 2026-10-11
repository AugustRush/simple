"""The channel registry as a plugin: it provides ``channels``.

Why this is a plugin
--------------------
A channel is a capability a plugin contributes -- that is what
:class:`~agent.channels.registry.ChannelRegistry` was built for -- but the registry
itself was provided by the composition root, on a **host of its own**:
``build_gateway_channels`` created a local ``PluginHost``, provided ``cfg`` and
``channels`` on it, and mounted the web and Feishu plugins there.  So the channel
plugins were plugins of a second, throwaway tree:

* the main catalog could not answer ``who_provides("channels")``;
* a profile could neither disable nor replace a channel, because the profile
  selects what the *main* composition mounts;
* the channel plugins could inject nothing but ``cfg`` and ``channels`` -- not the
  registry, not memory;
* their registrations were never unwound, because nothing disposed that host.

Providing the registry here puts them in the same tree as everything else.  The
plugin needs nothing, which is why it looked like composition code; it is a plugin
for the same reason ``registry`` is one: a service other plugins write into is
still a service.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ChannelsPlugin"]


class ChannelsPlugin:
    """Provides ``channels``: the registry channel plugins contribute to."""

    name = "channels"

    #: Nothing. A collection has no dependencies -- the *contributors* declare
    #: ``inject = ("channels", ...)`` and the kernel pends them until this runs.
    inject = ()

    def setup(self, ctx: Any) -> None:
        from .registry import ChannelRegistry

        ctx.provide("channels", ChannelRegistry())

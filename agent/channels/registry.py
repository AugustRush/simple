"""Channels as a capability a plugin contributes.

The gap this closes
-------------------
A plugin could contribute a tool, a slash command, a sub-agent role, an MCP config and a
skill root.  It could not contribute a **channel** -- the gateway's set was a hardcoded
function in ``bootstrap`` with a branch per built-in, so a third party wanting a channel
had to edit the composition root.  Web and Feishu are the two that got a branch.

With the registry, a channel is something a plugin registers on a service, like
everything else.  The two built-ins are the first users rather than special cases, and
:func:`agent.bootstrap.build_gateway_channels` reads what they registered instead of
deciding what exists.

A list, not a mapping
---------------------
Channels are ordered and unnamed to their owner: a composition may run two web channels
on different ports, and a registry keyed by name would refuse the second.  Each record
keeps its contributor, so ``/channels`` can say whose it is and a scope can take back
exactly what it added.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ChannelRegistry", "ScopedChannels"]


class ChannelRegistry:
    """The channels a composition runs, in the order they were contributed."""

    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []

    # -- writing -------------------------------------------------------------

    def add(self, channel: Any, *, owner: str = "") -> Any:
        """Register *channel*, returning the undo that removes it.

        By identity rather than by index: two channels may compare equal -- a web channel
        built twice from the same config -- and an undo that removed the wrong one would
        silently stop a transport that a plugin still wants.
        """
        record = {"channel": channel, "owner": owner}
        self._records.append(record)

        def forget() -> None:
            for index, item in enumerate(self._records):
                if item is record:
                    del self._records[index]
                    return

        return forget

    def remove(self, channel: Any) -> None:
        for index, item in enumerate(self._records):
            if item["channel"] is channel:
                del self._records[index]
                return

    def clear(self) -> None:
        self._records.clear()

    # -- reading -------------------------------------------------------------

    def all(self) -> tuple[Any, ...]:
        return tuple(item["channel"] for item in self._records)

    def owners(self) -> tuple[str, ...]:
        return tuple(item["owner"] for item in self._records)

    def __len__(self) -> int:
        return len(self._records)

    # -- snapshot, for a reload that has to roll back ------------------------

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._records]

    def restore(self, snapshot: list[dict[str, Any]]) -> None:
        self._records = [dict(item) for item in snapshot]

    # -- the scoped view -----------------------------------------------------

    def scoped(self, ctx: Any) -> "ScopedChannels":
        return ScopedChannels(self, ctx)


class ScopedChannels:
    """One plugin's handle on the registry."""

    __slots__ = ("_registry", "_ctx")

    def __init__(self, registry: ChannelRegistry, ctx: Any) -> None:
        self._registry = registry
        self._ctx = ctx

    @property
    def owner(self) -> str:
        return str(getattr(self._ctx, "name", "") or "")

    def add(self, channel: Any) -> Any:
        """Contribute one channel, removed when this plugin unloads."""
        undo = self._registry.add(channel, owner=self.owner)
        self._ctx.on_unwind(undo)
        return channel

"""The command registry's protocols.

``commands`` is provided by the **loader** (``agent/plugins/catalog.py``), not by a
plugin, and that is a *floor* with a reason -- the same kind of reason that makes
``plugin_catalog`` itself one:

* the loader registers a bundle's slash commands **while it loads that bundle**, so the
  registry has to exist before the plugin that declares them is activated;
* a plugin reload snapshots and restores the registry when its rollback path fires, and
  a registry a plugin could replace mid-reload is a registry the loader cannot roll
  back.

A floor still has an interface, and writing it down keeps the two roles apart: the
**service** belongs to the loader, and the **view** (:class:`CommandContributionView`,
what ``ctx.commands`` is) belongs to whichever plugin contributes a command.  A
replacement for *this* service is a different loader, not a plugin -- which is worth
saying out loud rather than leaving to be inferred from an import.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Protocol, runtime_checkable

__all__ = ["CommandContributionView", "CommandRegistryService"]


@runtime_checkable
class CommandContributionView(Protocol):
    """One plugin's handle on the command registry."""

    def add(
        self,
        name: str,
        handler: "Callable[..., Any]",
        *,
        metadata: "Optional[dict]" = None,
    ) -> str:
        """Contribute one slash command, removed when this plugin unloads."""


@runtime_checkable
class CommandRegistryService(Protocol):
    """The command registry the loader provides as ``commands``."""

    def add(
        self,
        name: str,
        handler: "Callable[..., Any]",
        *,
        metadata: "Optional[dict]" = None,
        owner: str = "",
    ) -> "Callable[[], None]":
        """Register *name*, returning the undo that removes it."""

    def clear(self) -> None:
        """Forget every command -- discovery starts from an empty registry."""

    def has(self, name: str) -> bool:
        """Whether *name* is taken (the loader refuses to shadow one silently)."""

    def owner(self, name: str) -> str:
        """Which plugin contributed *name*, for the clash message and for reports."""

    def handlers(self) -> dict:
        """The dispatch table the router uses."""

    def metadata(self) -> dict:
        """The per-command metadata the routes and the UI read."""

    def snapshot(self) -> "dict[str, dict[str, Any]]":
        """A restore point for the reload rollback path."""

    def restore(self, snapshot: "dict[str, dict[str, Any]]") -> None:
        """Put a snapshot back."""

    def scoped(self, ctx: Any) -> CommandContributionView:
        """The per-plugin view (the kernel calls this by name for ``ctx.commands``)."""

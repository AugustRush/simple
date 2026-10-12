"""The plugin loader's service protocol.

``plugin_catalog`` is a **floor** in the ownership ledger: the loader cannot be provided
by a plugin it has not loaded yet, so ``agent/bootstrap.py`` builds it and mounts the
rest through it.  That is a reason for it not being a *contribution*, not a reason for
its interface to be unwritten -- fifteen members are called by the composition, the
reload path, the prompt assembler, the runtime's hook firers and the command layer, and
every one of them is a thing a different loader would also have to offer.

The three groups the members fall into are worth naming, because they are three
different relations to the tree:

* **mounting** -- ``mount`` (a plugin the catalog did not discover),
  ``discover_and_load``, ``flush_pending`` (activate what was waiting on a service the
  root wrote after the mounts), ``close``;
* **declaring** -- ``core_plugin_selection`` and ``service_replacements`` (what the
  active profile says the composition is), ``who_provides`` (the answer a replacement
  is checked against);
* **reading and reporting** -- ``pending_report``, ``failed_report``,
  ``compose_all_prompts``, and the three
  command-hook firers (``fire_prompt_submit``, ``fire_turn_end``, ``fire_session_end``).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - types only
    from agent.core.events import SessionEvent, TurnEvent
    from agent.plugins.catalog import HookResult

__all__ = ["PluginCatalogService"]


@runtime_checkable
class PluginCatalogService(Protocol):
    """The plugin loader a composition provides as ``plugin_catalog``."""

    # -- mounting -----------------------------------------------------------
    def mount(self, plugin: Any) -> Any:
        """Mount a plugin the catalog did not discover; returns its install."""

    def discover_and_load(self) -> "list[str]":
        """Load everything on disk; returns the names that are mounted."""

    def flush_pending(self) -> "tuple[str, ...]":
        """Activate what can be activated now; returns the names that just did.

        Not the names still waiting: the caller asks "did anything come up", and the
        waiters are what ``pending_report`` is for.
        """

    def close(self) -> None:
        """Unload the tree.  Safe to call twice."""

    # -- declaring ----------------------------------------------------------
    def core_plugin_selection(self) -> "Optional[tuple[str, ...]]":
        """Which of the composition's own plugins the active profile includes."""

    def service_replacements(self) -> "dict[str, str]":
        """Service name -> the plugin that provides it instead of the built-in one."""

    def who_provides(self, service: str) -> "Optional[str]":
        """Which plugin provided *service*, or ``None`` if nothing did."""

    # -- reading and reporting ----------------------------------------------
    def pending_report(self) -> "dict[str, list[str]]":
        """Plugin -> what it is still waiting for."""

    def failed_report(self) -> "dict[str, str]":
        """Plugin -> why its setup raised."""

    def compose_all_prompts(self, base: str) -> str:
        """Let every plugin append to the system prompt, in load order."""

    async def fire_prompt_submit(self, text: str, metadata: "Optional[dict]" = None) -> "HookResult":
        """Run the ``on_prompt_submit`` command hooks (a gate: the first block wins)."""

    async def fire_turn_end(self, event: "TurnEvent") -> "list[HookResult]":
        """Run the ``on_turn_end`` hooks; a ``continue`` result asks for another turn."""

    async def fire_session_end(self, event: "SessionEvent") -> None:
        """Tell plugins the session ended."""

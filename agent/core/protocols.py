"""The agent service's protocol.

``agent`` is provided by ``agent/core/agent_plugin.py`` and read by the composition,
both channels, the CLI, the command layer, the turn runner, the evolution plugin, Ralph
and the scheduler.  Its interface is *two* things, and both are visible to a caller:

* **what it answers** -- ``endpoint_for`` (which endpoint serves a model),
  ``consolidation_model``/``consolidation_endpoint`` (what the memory work runs on),
  and ``send_message`` (run one turn);
* **what it *is*, as data** -- ``api_format``, ``max_tokens``, ``context_window``,
  ``supports_vision`` and ``content_filter``.  These are read, not called, so a
  method-only protocol would let a replacement satisfy every signature and then fail
  on the first attribute read.  ``tests/test_memory_contract.py``'s attribute closure
  guard exists for exactly that mistake, made once already in this refactor.

``register_spawn_capability`` is here because the composition calls it after mounting
(composition knowledge the agent cannot read for itself), which makes it part of what
a replacement has to accept -- not an optional extra.

What is *not* here: everything the agent's own implementation modules use
(``_transport``, ``registry``, ``context_manager``, the settings the loop reads from
itself).  Those are the implementation's business; only the composition hands them in,
through the constructor.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - types only
    from agent.core.agent import AgentContext, AgentResult
    from agent.core.transport import ModelEndpoint

__all__ = ["AgentContributionView", "AgentRegistryService", "AgentService"]


@runtime_checkable
class AgentService(Protocol):
    """The agent a composition provides as ``agent``."""

    # -- data a caller reads ------------------------------------------------
    api_format: str
    max_tokens: int
    context_window: "Optional[int]"
    supports_vision: bool
    content_filter: Any

    # -- what it answers ----------------------------------------------------
    def endpoint_for(self, model: "Optional[str]" = None) -> "ModelEndpoint":
        """The endpoint that serves *model*, routed through this agent's transport."""

    def consolidation_model(self, cfg: dict) -> str:
        """The model this agent's memory consolidation runs on, per *cfg*."""

    def consolidation_endpoint(self, cfg: dict) -> "ModelEndpoint":
        """The endpoint that goes with :meth:`consolidation_model`."""

    async def send_message(
        self,
        ctx: "AgentContext",
        user_message: str,
        stream_callback: "Optional[Callable[[str], None]]" = None,
        attachments: "tuple[Any, ...]" = (),
    ) -> "AgentResult":
        """Run one turn against *ctx*."""

    # -- what the composition tells it --------------------------------------
    def register_spawn_capability(
        self, base_system_prompt: str, workspace_root: "Optional[Path]" = None
    ) -> None:
        """Teach the agent the spawn capability the composition knows about."""


@runtime_checkable
class AgentContributionView(Protocol):
    """One plugin's handle on the sub-agent role registry."""

    def add(self, name: str, definition: dict) -> str:
        """Contribute one role, returning its namespaced key; unwound with the plugin."""


@runtime_checkable
class AgentRegistryService(Protocol):
    """The role registry the loader provides as ``agents``.

    A **floor**, for the same two reasons as ``commands`` (see
    ``agent/commands/protocols.py``): the loader registers a bundle's roles while it
    loads that bundle, and the reload rollback restores this registry -- so it must
    exist before any plugin activates and it cannot be swapped out mid-reload.
    """

    def add(self, key: str, definition: dict) -> "Any":
        """Register a role under its full key, returning the undo."""

    def clear(self) -> None:
        """Forget every role -- discovery starts from an empty registry."""

    def get(self, key: str) -> "Any":
        """One role definition, or ``None``."""

    def all(self) -> "Any":
        """Every role, for the sub-agent listing."""

    def snapshot(self) -> "Any":
        """A restore point for the reload rollback path."""

    def restore(self, snapshot: "Any") -> None:
        """Put a snapshot back."""

    def scoped(self, ctx: Any) -> AgentContributionView:
        """The per-plugin view (the kernel calls this by name for ``ctx.agents``)."""

"""The skill layer's service protocol.

``skill_catalog`` is provided by ``agent/skills/plugin.py`` and read by twelve
modules, but its interface was whatever those modules happened to call.  This is that
surface, written down -- see :mod:`agent.tools.protocols` for why the protocols live
beside their services and what the ledger's ``contract`` column means.

What is here is what a caller does with the catalog, in the three shapes those callers
have: ask it things (``list_skills``, ``find_any``, ``activation_text``,
``summary_lines``), change it (``forget``), and tell it that the world moved
(``reload``, ``load_all``, ``invalidate``, ``consume_dirty``).  The last two are how the
prompt and the plugin catalog learn that skills changed on disk, which is why they are
part of the interface rather than private: a replacement that cannot answer "did
anything change?" would silently freeze the skill list for the life of the process.

Members the class also has, and why they are *not* here: ``get``/``is_enabled``/
``resolve_ref``/``refresh_if_stale``/``register_tools`` have no caller outside the
implementation and its own plugin, and ``set_enabled`` is called only on the
*scheduler's* store, which is a different object with a same-named method -- the
mistake a name-based scan makes, and the reason this list was verified by reading the
call sites rather than by trusting the scan.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - types only
    from agent.skills.catalog import SkillBundle

__all__ = ["SkillCatalogService", "SkillContributionView"]


@runtime_checkable
class SkillContributionView(Protocol):
    """One plugin's handle on the catalog: how a plugin contributes its own skills.

    A bundle that ships a ``skills/`` directory contributes it here, in its ``setup``,
    through the injected service.  The view owns attribution (the source names the
    plugin) and the undo (the root leaves with the plugin's scope), which is the same
    shape ``channels`` and ``registry`` have -- one contribution mechanism, three
    capability services.
    """

    def add_root(
        self,
        root: "Path | str",
        *,
        source: str = "",
        keep_existing: bool = False,
        skip_dirs: "tuple[str, ...]" = (),
    ) -> int:
        """Attach a skill root, returning how many skills it contributed."""


@runtime_checkable
class SkillCatalogService(Protocol):
    """The skill catalog a composition provides as ``skill_catalog``."""

    def scoped(self, ctx: Any) -> SkillContributionView:
        """The per-plugin view (the kernel calls this by name for ``ctx.skill_catalog``)."""

    def skill_count(self) -> int:
        """How many skills are loaded -- what a contributor reports it added."""

    # ``contributed_roots()`` is deliberately *not* here yet: a protocol is the
    # contract a production caller uses, and today nothing in ``agent/`` reads it --
    # tests do, to assert a contribution.  When a screen or a report wants "which
    # roots are attached, by whom", it joins the protocol with its caller.

    def load_all(self) -> None:
        """Read every root again (builtin, the user's, the shared ones)."""

    def reload(self) -> None:
        """Re-read after something on disk changed, keeping contributors intact."""

    def invalidate(self) -> None:
        """Mark the prompt's skill section stale, without re-reading anything."""

    def consume_dirty(self) -> bool:
        """Whether the prompt should be rebuilt -- and clears the flag."""

    def list_skills(self) -> "list[SkillBundle]":
        """The skills a turn may use, enabled and model-invocable ones included."""

    def find_any(self, skill_ref: str) -> "Optional[SkillBundle]":
        """The bundle for an id or alias, switched-off ones included."""

    def activation_text(self, skill_ref: str, *, explicit: bool = False) -> "Optional[str]":
        """The instructions to inject for one skill, or ``None``."""

    def summary_lines(self) -> "list[str]":
        """The compact list that goes into the system prompt."""

    def forget(self, skill_id: str) -> None:
        """Drop a skill from the catalog (a delete the caller already did on disk)."""

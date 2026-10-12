"""The skill catalog, seen by one plugin.

The same shape as ``agent/channels/registry.py``'s ``ScopedChannels``: a capability
service hands each plugin a per-plugin view, the view attributes what the plugin
attaches to it, and the attachment is undone with the plugin's scope.  A plugin that
bundles skills therefore contributes them the way a plugin contributes a channel or a
tool -- through an injected service, not by being read out of the loader's bookkeeping
by the composition root afterwards.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["ScopedSkillCatalog"]


class ScopedSkillCatalog:
    """One plugin's handle on the skill catalog."""

    __slots__ = ("_catalog", "_ctx")

    def __init__(self, catalog: Any, ctx: Any) -> None:
        self._catalog = catalog
        self._ctx = ctx

    @property
    def owner(self) -> str:
        return str(getattr(self._ctx, "name", "") or "")

    def __getattr__(self, item: str) -> Any:
        """Reads pass through to the catalog.

        The kernel hands ``ctx.skill_catalog`` (and ``ctx.get("skill_catalog")``) to every
        plugin that injects the service, so a plugin that only *reads* the catalog -- ralph
        looks a skill up by id -- would lose it the moment the service grew a contribution
        view.  A view is therefore the service **plus** attribution, not a narrower
        service: reading is not a contribution and needs no accounting, while the calls
        this view owns (``add_root``) stay attributed and unwound.
        """
        return getattr(self._catalog, item)

    def add_root(
        self,
        root: "Path | str",
        *,
        source: str = "",
        keep_existing: bool = False,
        skip_dirs: "tuple[str, ...]" = (),
    ) -> int:
        """Contribute a skill root; it leaves when this plugin unloads.

        Returns how many skills the root contributed, which is what a plugin reports
        and what a test asserts -- the alternative (returning the undo) would invite
        callers to unwind by hand and leave the scope's copy behind.

        The source defaults to ``plugin:<owner>``: attribution is the view's job, and
        a contributor that has to remember to name itself is a contributor that can
        forget.
        """
        before = self._catalog.skill_count()
        undo = self._catalog.add_root(
            Path(root),
            source=source or f"plugin:{self.owner}",
            keep_existing=keep_existing,
            skip_dirs=skip_dirs,
        )
        self._ctx.on_unwind(undo)
        return self._catalog.skill_count() - before

"""The built-in tool set as a plugin, so a composition can replace it.

Why this is the interesting one
-------------------------------
``BuiltinTools`` is the largest implementation in the project and it was the last thing
``bootstrap`` still constructed **directly** -- ``builtin_tools_cls(memory, registry, ...)``
at the composition root.  Every tool it registers was therefore permanent for the life of
the process: no composition could swap the tool set, and none could remove it.

As a plugin it inherits what the kernel already does for everything else.  The class is
passed in rather than imported at the point of use, so ``bootstrap.BuiltinTools`` stays the
substitution point; the registry arrives as ``ctx.tools``, so every registration lands on
this plugin's scope and **unloading the plugin removes the tools**; and the plugin declares
what it needs instead of being handed the mapping.

The source tag stays ``builtin``
--------------------------------
Each registration in ``BuiltinTools`` passes ``source="builtin"`` explicitly, and
``ToolRegistry`` keys its capability table on ``(source, name)`` --
``("builtin", "read_file"): frozenset({"read"})``.  The scoped view honours an explicit
source rather than stamping the plugin's own name over it, which is why those entries keep
resolving.  Without that, moving this class behind a view would have silently emptied the
capability table.
"""

from __future__ import annotations

from typing import Any

from agent.tools.builtin_tools import BuiltinTools as _DefaultTools

__all__ = ["ToolsPlugin"]


class ToolsPlugin:
    """Registers the built-in tools on this plugin's scope."""

    name = "tools"

    #: Everything ``BuiltinTools`` is constructed with.  ``registry`` arrives as the scoped
    #: view because the registrations have to be undoable, and the rest are the services
    #: the tool implementations read.
    inject = (
        "cfg",
        "registry",
        "memory",
        "context_manager",
        "workspace_root",
        "output_dir",
        "file_service",
    )

    def __init__(self, tools_class: Any = _DefaultTools) -> None:
        self._tools_class = tools_class

    def setup(self, ctx: Any) -> None:
        from agent import shared
        from agent.memory._helpers import normalize_memory_chapter

        aliases = shared.LEGACY_MEMORY_ALIASES
        self._tools_class(
            ctx.get("memory"),
            ctx.get("registry"),
            context_manager=ctx.get("context_manager"),
            workspace_root=ctx.get("workspace_root"),
            chapter_normalizer=lambda chapter: normalize_memory_chapter(chapter, aliases),
            output_dir=ctx.get("output_dir"),
            file_service=ctx.get("file_service"),
        )

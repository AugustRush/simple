"""The catalog's declarative loaders, and the undos they hand back.

A plugin's ``commands/*.md`` and ``agents/*.md`` files are registered by the loader,
which returns one undo per registration rather than letting the unload path filter its
lists by plugin name.  That is what these tests check: that the loader can reverse what
it just did, including the case of two files where a late-bound closure would remove
only the last one.

The rest of what this file used to cover -- a ``PluginAssets`` contribution service
with ``add_slash_command`` and friends -- is gone.  Measurement showed no plugin ever
called it: its only caller was the catalog's own loader, and two of its five methods had
no caller at all.  Commands and roles are contributed through ``ctx.commands`` and
``ctx.agents``, which those registries' own tests cover.
"""

from __future__ import annotations

from pathlib import Path

import pytest


class _Catalog:
    """A stand-in for the plugin catalog, carrying only what assets touch.

    Deliberately not the real catalog: this file is about the contribution service,
    and the catalog's discovery machinery is a different concern with its own 139
    tests.
    """

    def __init__(self, tool_registry=None) -> None:
        # One registry rather than three parallel dicts; see
        # agent/commands/registry.py.
        from agent.commands.registry import CommandRegistry

        self._commands = CommandRegistry()
        self._bundled_skills: list = []
        self._bundled_mcp: list = []
        from agent.core.agent_registry import AgentRegistry

        self._agent_defs = AgentRegistry()
        self._tool_registry = tool_registry


def _assets(catalog: _Catalog, owner: str):
    """A view for a plugin called *owner*, plus the undos it registered."""
    from agent.plugins.assets import PluginAssets

    undos: list = []

    class _Ctx:
        name = owner

        def on_unwind(self, handler):
            undos.append(handler)

    return PluginAssets(catalog).scoped(_Ctx()), undos

# ── Slash commands ───────────────────────────────────────────────────────────


# ── Bundled skills and MCP configs ───────────────────────────────────────────


# ── Agent definitions ────────────────────────────────────────────────────────


# ── Tools ────────────────────────────────────────────────────────────────────


# ── The convention the kernel relies on ──────────────────────────────────────


# ── The declarative loaders return their undos ───────────────────────────────


def _catalog_paths(tmp_path):
    """A plugin directory with one command file and one agent file."""
    commands = tmp_path / "commands"
    commands.mkdir()
    (commands / "review.md").write_text(
        "---\ndescription: reviews\n---\nReview $ARGUMENTS\n", encoding="utf-8"
    )
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "auditor.md").write_text(
        "---\nname: auditor\ndescription: audits\n---\nAudit things\n", encoding="utf-8"
    )
    return tmp_path

class _Ctx:
    """The two things a contribution view needs: a name, and an undo sink.

    The loader methods take the **view** now.  Their undo used to be returned to the
    caller, who had to remember to register it -- which is the bookkeeping a scope
    exists to remove, and it made these two kinds the only ones that did not contribute
    the way every other kind does.  The view records the undo on the ctx it was built
    with, so these tests hold the ctx instead of an undo list.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.undos: list = []

    def on_unwind(self, handler) -> None:
        self.undos.append(handler)

    def unwind(self) -> None:
        for undo in reversed(self.undos):
            undo()


def test_command_files_are_contributed_through_the_view(tmp_path) -> None:
    """The loader registers through the plugin's view, and the view owns the undo."""
    from agent.commands.registry import ScopedCommands
    from agent.plugins.catalog import PluginCatalog

    root = _catalog_paths(tmp_path)
    catalog = PluginCatalog(builtin_dir=tmp_path)
    ctx = _Ctx("p")
    try:
        keys = catalog._load_command_files(
            root, "p", None, ScopedCommands(catalog._commands, ctx)
        )

        assert keys == ["p:review"], "it reports what it registered"
        assert catalog._commands.has("p:review")
        assert catalog._commands.owner("p:review") == "p", "attributed to the contributor"

        ctx.unwind()

        assert not catalog._commands.has("p:review")
        assert "p:review" not in catalog._commands.metadata()
        assert catalog._commands.owner("p:review") == "", "attribution went with it"
    finally:
        catalog.close()


def test_agent_files_are_contributed_through_the_view(tmp_path) -> None:
    from agent.core.agent_registry import ScopedAgentDefs
    from agent.plugins.catalog import PluginCatalog

    root = _catalog_paths(tmp_path)
    catalog = PluginCatalog(builtin_dir=tmp_path)
    ctx = _Ctx("p")
    try:
        keys = catalog._load_agent_files(
            root, "p", None, ScopedAgentDefs(catalog._agent_defs, ctx)
        )

        assert keys == ["plugin:p:auditor"], "the view namespaces and reports the key"
        assert catalog._agent_defs.get("plugin:p:auditor") is not None

        ctx.unwind()

        assert catalog._agent_defs.get("plugin:p:auditor") is None
    finally:
        catalog.close()


def test_two_agent_files_are_removed_independently(tmp_path) -> None:
    """Two contributions under one view unwind one record at a time."""
    from agent.core.agent_registry import ScopedAgentDefs
    from agent.plugins.catalog import PluginCatalog

    agents = tmp_path / "agents"
    agents.mkdir()
    for stem in ("one", "two"):
        (agents / f"{stem}.md").write_text(
            f"---\nname: {stem}\n---\nbody\n", encoding="utf-8"
        )
    catalog = PluginCatalog(builtin_dir=tmp_path)
    ctx = _Ctx("p")
    try:
        keys = catalog._load_agent_files(
            tmp_path, "p", None, ScopedAgentDefs(catalog._agent_defs, ctx)
        )
        assert len(keys) == 2

        # One record at a time: the first undo removes its own record and no other.
        ctx.undos[0]()
        remaining = set(catalog._agent_defs.keys())
        assert len(remaining) == 1, f"exactly one should remain, got {remaining}"

        ctx.undos[1]()
        assert set(catalog._agent_defs.keys()) == set()
    finally:
        catalog.close()


def test_no_files_contributes_nothing(tmp_path) -> None:
    """A plugin with no declarative assets registers no key and records no undo."""
    from agent.commands.registry import ScopedCommands
    from agent.core.agent_registry import ScopedAgentDefs
    from agent.plugins.catalog import PluginCatalog

    empty = tmp_path / "empty"
    empty.mkdir()
    catalog = PluginCatalog(builtin_dir=tmp_path)
    ctx = _Ctx("p")
    try:
        assert (
            catalog._load_command_files(
                empty, "p", None, ScopedCommands(catalog._commands, ctx)
            )
            == []
        )
        assert (
            catalog._load_agent_files(
                empty, "p", None, ScopedAgentDefs(catalog._agent_defs, ctx)
            )
            == []
        )
        assert ctx.undos == []
    finally:
        catalog.close()

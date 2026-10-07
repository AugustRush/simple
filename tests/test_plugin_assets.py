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

def test_command_files_return_an_undo_each(tmp_path) -> None:
    """The loader can reverse what it registered, without knowing the plugin name."""
    from agent.plugins.catalog import PluginCatalog

    root = _catalog_paths(tmp_path)
    catalog = PluginCatalog(builtin_dir=tmp_path)
    try:
        # The owners map is gone as a parameter: the registry records the owner with
        # the handler, so there is no second structure to keep in step.
        undos = catalog._load_command_files(root, "p")
        assert len(undos) == 1
        assert catalog._commands.has("p:review")
        assert catalog._commands.owner("p:review") == "p"

        undos[0]()
        assert not catalog._commands.has("p:review")
        assert "p:review" not in catalog._commands.metadata()
        assert catalog._commands.owner("p:review") == "", "attribution went with it"
    finally:
        catalog.close()

def test_agent_files_return_an_undo_each(tmp_path) -> None:
    from agent.plugins.catalog import PluginCatalog

    root = _catalog_paths(tmp_path)
    catalog = PluginCatalog(builtin_dir=tmp_path)
    try:
        undos = catalog._load_agent_files(root, "p")
        assert len(undos) == 1
        assert catalog._agent_defs.get("plugin:p:auditor") is not None

        undos[0]()
        assert catalog._agent_defs.get("plugin:p:auditor") is None
    finally:
        catalog.close()

def test_two_agent_files_get_independent_undos(tmp_path) -> None:
    """The closure must bind per iteration, not share the loop variable.

    A late-bound key removes only the last definition, which stays invisible until
    a plugin contributes two agents -- so it is asserted with two.
    """
    from agent.plugins.catalog import PluginCatalog

    agents = tmp_path / "agents"
    agents.mkdir()
    for stem in ("one", "two"):
        (agents / f"{stem}.md").write_text(
            f"---\nname: {stem}\n---\nbody\n", encoding="utf-8"
        )
    catalog = PluginCatalog(builtin_dir=tmp_path)
    try:
        undos = catalog._load_agent_files(tmp_path, "p")
        assert len(undos) == 2
        undos[0]()
        remaining = set(catalog._agent_defs.keys())
        assert len(remaining) == 1, f"exactly one should remain, got {remaining}"
    finally:
        catalog.close()

def test_no_files_returns_no_undos(tmp_path) -> None:
    """A plugin with no declarative assets contributes nothing to undo."""
    from agent.plugins.catalog import PluginCatalog

    empty = tmp_path / "empty"
    empty.mkdir()
    catalog = PluginCatalog(builtin_dir=tmp_path)
    try:
        assert catalog._load_command_files(empty, "p", {}) == []
        assert catalog._load_agent_files(empty, "p") == []
    finally:
        catalog.close()

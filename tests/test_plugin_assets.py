"""Contributions as a service: the first real user of a scoped view.

``PluginAssets`` is what a plugin reaches to contribute a slash command, a skills
root, an MCP server or an agent definition.  It used to be four methods on the
context every plugin was handed, which meant the *plugin interface* knew what a
slash command is -- the coupling the kernel exists to avoid.  As a service, the
catalog provides it and the kernel never learns the word.

The mechanism under test is the kernel's: a service exposing ``scoped(ctx)`` is
asked for a per-plugin view, and a mutation through that view registers its own
undo on the calling plugin's lifetime.  The point here is that the contribution
service is the first thing to actually need it -- so if the convention is awkward,
this file is where that shows.
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


def test_a_slash_command_is_registered_and_attributed() -> None:
    catalog = _Catalog()
    assets, _ = _assets(catalog, "github")
    assets.add_slash_command("pr", lambda **kw: "ok")
    assert catalog._commands.has("pr")
    assert catalog._commands.owner("pr") == "github", "attributed for reporting"
    assert "pr" in catalog._commands.metadata()


def test_removing_a_slash_command_undoes_all_three_writes() -> None:
    """A contribution that writes three places must remove all three.

    The failure this prevents is a command that is gone but still listed as owned,
    so a later plugin's conflict report names a plugin that no longer exists.
    """
    catalog = _Catalog()
    assets, undos = _assets(catalog, "github")
    assets.add_slash_command("pr", lambda **kw: "ok")
    assert len(undos) == 1
    undos[0]()
    assert catalog._commands.names() == ()
    assert catalog._commands.metadata() == {}


def test_the_command_name_is_returned() -> None:
    """So a caller can log or chain it without repeating the string."""
    assets, _ = _assets(_Catalog(), "p")
    assert assets.add_slash_command("x", lambda **kw: None) == "x"


# ── Bundled skills and MCP configs ───────────────────────────────────────────


def test_a_bundled_skill_is_registered_with_its_owner() -> None:
    catalog = _Catalog()
    assets, _ = _assets(catalog, "github")
    assets.add_bundled_skill(Path("/tmp/skills"))
    assert catalog._bundled_skills == [("github", Path("/tmp/skills"))]


def test_removing_a_skill_leaves_another_plugins_entry_alone() -> None:
    """Identity, not equality: two plugins may contribute an equal-looking root."""
    catalog = _Catalog()
    mine, my_undos = _assets(catalog, "mine")
    theirs, _ = _assets(catalog, "theirs")
    mine.add_bundled_skill(Path("/tmp/skills"))
    theirs.add_bundled_skill(Path("/tmp/skills"))
    assert len(catalog._bundled_skills) == 2

    my_undos[0]()
    assert catalog._bundled_skills == [("theirs", Path("/tmp/skills"))]


def test_an_mcp_config_is_registered_and_removed() -> None:
    catalog = _Catalog()
    assets, undos = _assets(catalog, "github")
    assets.add_bundled_mcp({"name": "gh"})
    assert catalog._bundled_mcp == [("github", {"name": "gh"})]
    undos[0]()
    assert catalog._bundled_mcp == []


def test_two_equal_mcp_configs_are_removed_independently() -> None:
    catalog = _Catalog()
    mine, my_undos = _assets(catalog, "mine")
    theirs, _ = _assets(catalog, "theirs")
    mine.add_bundled_mcp({"name": "same"})
    theirs.add_bundled_mcp({"name": "same"})
    my_undos[0]()
    assert catalog._bundled_mcp == [("theirs", {"name": "same"})]


# ── Agent definitions ────────────────────────────────────────────────────────


def test_an_agent_definition_is_keyed_the_way_the_catalog_reads_it() -> None:
    catalog = _Catalog()
    assets, _ = _assets(catalog, "github")
    assets.add_agent_definition("reviewer", {"description": "reviews"})
    assert catalog._agent_defs.get("plugin:github:reviewer") is not None
    assert catalog._agent_defs.get("plugin:github:reviewer")["plugin"] == "github"


def test_removing_an_agent_definition_removes_only_that_key() -> None:
    catalog = _Catalog()
    mine, my_undos = _assets(catalog, "mine")
    theirs, _ = _assets(catalog, "theirs")
    mine.add_agent_definition("reviewer", {})
    theirs.add_agent_definition("reviewer", {})
    my_undos[0]()
    assert catalog._agent_defs.get("plugin:mine:reviewer") is None
    assert catalog._agent_defs.get("plugin:theirs:reviewer") is not None


# ── Tools ────────────────────────────────────────────────────────────────────


def test_a_tool_is_registered_when_a_registry_is_mounted() -> None:
    from agent.tools.runtime import ToolRegistry

    registry = ToolRegistry()
    catalog = _Catalog(tool_registry=registry)
    assets, _ = _assets(catalog, "github")

    class _Tool:
        name = "create_issue"
        description = "opens an issue"
        parameters = {"type": "object", "properties": {}}

        def fn(self):
            return "ok"

    assets.add_tool(_Tool())
    assert "create_issue" in registry.list_tools()


def test_a_tool_contribution_without_a_registry_is_a_no_op() -> None:
    """A plugin must stay loadable in a composition that does not mount tools."""
    assets, undos = _assets(_Catalog(tool_registry=None), "github")

    class _Tool:
        name = "create_issue"

    assets.add_tool(_Tool())  # must not raise
    assert undos == [], "nothing was registered, so nothing needs undoing"


def test_removing_a_tool_undoes_the_whole_group() -> None:
    from agent.tools.runtime import ToolRegistry

    registry = ToolRegistry()
    catalog = _Catalog(tool_registry=registry)
    assets, undos = _assets(catalog, "github")

    class _Tool:
        def __init__(self, name):
            self.name = name
            self.description = ""
            self.parameters = {"type": "object", "properties": {}}

        def fn(self):
            return "ok"

    assets.add_tool(_Tool("one"))
    assets.add_tool(_Tool("two"))
    assert {"one", "two"}.issubset(set(registry.list_tools()))

    for undo in undos:
        undo()
    assert "one" not in registry.list_tools()
    assert "two" not in registry.list_tools()


# ── The convention the kernel relies on ──────────────────────────────────────


def test_the_service_exposes_the_scoped_factory() -> None:
    """Without it the kernel hands over the raw service and accounting is lost."""
    from agent.plugins.assets import PluginAssets

    service = PluginAssets(_Catalog())
    assert callable(service.scoped)


def test_each_view_is_bound_to_the_context_it_was_made_for() -> None:
    """Two plugins contributing the same name do not share an owner."""
    from agent.plugins.assets import PluginAssets

    catalog = _Catalog()
    service = PluginAssets(catalog)
    mine, _ = _assets(catalog, "mine")
    theirs, _ = _assets(catalog, "theirs")
    mine.add_slash_command("same", lambda **kw: None)
    assert catalog._commands.owner("same") == "mine"
    assert service.catalog is catalog


def test_the_owner_is_readable_for_reporting() -> None:
    """The catalog names the owner of a conflicting command; that name has to come
    from the view, since the view is all the plugin has."""
    assets, _ = _assets(_Catalog(), "github")
    assert assets.owner == "github"


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

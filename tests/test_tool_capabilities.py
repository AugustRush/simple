"""A capability a plugin contributes, and the undo that comes with it.

The plan's model is ``Plugin → Service / Event / Capability``, where a capability is
what a **service** exposes as callable rather than a universal abstraction.  A tool is
the clearest example: the plugin does not *become* a tool, it registers one on the
registry, and the registration goes away with the plugin.

The plan's own example is what these tests drive:

    class GitHubPlugin:
        inject = ["registry"]

        def setup(self, ctx):
            ctx.registry.register(CreateIssue(...))
            ctx.registry.register(CreatePR(...))

so the acceptance question is not "does the registry work" -- it has its own tests --
but "does reaching it through a context make the registrations reversible, and can a
plugin author tell what they registered".

A note on the service's name
----------------------------
The plan writes ``ctx.tools``; this codebase calls the service ``registry``.  The name
was left alone: it is read as a service in seven places but appears as a component key
in eighty-nine, and the *requirement* -- a plugin registers a capability on a service,
reversibly, without inventing a source string -- does not depend on the spelling.  The
plan's ``ctx.tools`` is illustrative of the shape, and the shape is what is tested
here.
"""

from __future__ import annotations

import pytest

from agent.kernel.plugin import PluginHost
from agent.tools.runtime import ToolRegistry


class _Tool:
    """A tool object: the shape a plugin contributes."""

    def __init__(self, name: str, *, description: str = "", capabilities=None) -> None:
        self.name = name
        self.description = description
        self.parameters = {"type": "object", "properties": {}}
        self.capabilities = capabilities

    def fn(self, **kwargs):
        return f"{self.name} ran"


def _host():
    registry = ToolRegistry()
    host = PluginHost()
    # The service the composition provides. `registry` is the key the runtime uses.
    host.services.provide("registry", registry)
    return host, registry


class _ToolPlugin:
    """A plugin that contributes tools the way the plan's example does."""

    name = "tool_plugin"
    inject = ("registry",)

    def __init__(self, *tool_names: str) -> None:
        self._names = tool_names

    def setup(self, ctx):
        self.ctx = ctx
        for tool_name in self._names:
            ctx.registry.register(_Tool(tool_name))


# ── The capability is contributed ────────────────────────────────────────────


def test_a_plugin_registers_a_tool_through_its_context() -> None:
    """The plan's example, verbatim in shape."""
    host, registry = _host()
    host.load(_ToolPlugin("create_issue"))
    assert "create_issue" in registry.list_tools()


def test_the_source_is_derived_from_the_plugin_not_invented() -> None:
    """``register`` takes a source string; a plugin author should not have to.

    The registry needs one to answer "who registered this" and to remove a group.
    Through a context it is the plugin's name, so the two cannot disagree -- which is
    the failure the string form invites: a source that says one thing while the
    unregister call says another.
    """
    host, registry = _host()
    host.load(_ToolPlugin("create_issue"))
    assert registry.tool_source("create_issue") == "plugin:tool_plugin"


def test_a_tool_object_carries_its_own_description_and_capabilities() -> None:
    host, registry = _host()

    class _Fancy(_Tool):
        def __init__(self):
            super().__init__("fancy", description="does fancy things", capabilities=["io"])

    class _P:
        name = "p"
        inject = ("registry",)

        def setup(self, ctx):
            ctx.registry.register(_Fancy())

    host.load(_P())
    assert set(registry.tool_capabilities("fancy")) == {"io"}


def test_a_tool_without_a_name_is_refused() -> None:
    """Named at registration, not at call time: a nameless tool is unreachable."""
    host, registry = _host()

    class _P:
        name = "p"
        inject = ("registry",)

        def setup(self, ctx):
            ctx.registry.register(object())

    install = host.load(_P())
    assert install.error, "a nameless tool must fail the plugin, not land silently"
    assert list(registry.list_tools()) == []


# ── The undo comes with it ───────────────────────────────────────────────────


def test_unloading_the_plugin_removes_its_tools() -> None:
    """The whole point: the plugin never names a source, and the tools still go."""
    host, registry = _host()
    host.load(_ToolPlugin("create_issue", "create_pr"))
    assert {"create_issue", "create_pr"}.issubset(set(registry.list_tools()))

    host.unload("tool_plugin")
    assert "create_issue" not in registry.list_tools()
    assert "create_pr" not in registry.list_tools()


def test_two_plugins_contribute_independently() -> None:
    """Each view records on its own plugin's lifetime, so unloads do not interact."""
    host, registry = _host()

    class _Other:
        name = "other"
        inject = ("registry",)

        def setup(self, ctx):
            ctx.registry.register(_Tool("other_tool"))

    host.load(_ToolPlugin("mine"))
    host.load(_Other())
    assert {"mine", "other_tool"}.issubset(set(registry.list_tools()))

    host.unload("tool_plugin")
    assert "mine" not in registry.list_tools()
    assert "other_tool" in registry.list_tools(), "the other plugin kept its own"


def test_a_plugin_that_raises_halfway_keeps_none_of_its_tools() -> None:
    """A failed setup disposes the scope, so the first tool goes with the second."""
    host, registry = _host()

    class _HalfBroken:
        name = "half"
        inject = ("registry",)

        def setup(self, ctx):
            ctx.registry.register(_Tool("first"))
            raise RuntimeError("setup exploded")

    install = host.load(_HalfBroken())
    assert install.error
    assert list(registry.list_tools()) == [], "a half-registered plugin is not a state"


def test_reloading_replaces_rather_than_colliding() -> None:
    """A plugin claiming its own name again is the ordinary reload, not a conflict.

    The registry refuses a duplicate by default, to catch two *different* sources
    claiming one name.  A plugin replacing itself is not that, so the scoped view
    defaults to replacing -- otherwise every plugin author would write ``replace=True``
    to say something the context already knows.
    """
    host, registry = _host()
    host.load(_ToolPlugin("create_issue"))
    host.load(_ToolPlugin("create_issue"), replace=True)
    assert "create_issue" in registry.list_tools()


def test_a_second_plugin_claiming_the_name_still_collides() -> None:
    """The registry's guard is not weakened by the scoped view's default."""
    host, registry = _host()
    host.load(_ToolPlugin("shared"))

    class _Thief:
        name = "thief"
        inject = ("registry",)

        def setup(self, ctx):
            ctx.registry.register(_Tool("shared"))

    install = host.load(_Thief())
    assert install.error, "a different source claiming the name must be refused"
    assert registry.tool_source("shared") == "plugin:tool_plugin"


# ── The contribution service delegates rather than duplicating ───────────────


def test_the_contribution_service_uses_the_same_mechanism() -> None:
    """One implementation of "register a tool and arrange for its removal".

    ``PluginAssets.add_tool`` is the name a plugin looks for on the catalog's
    contribution service.  It delegates to the registry's scoped view, so the removal
    logic exists once -- a second copy is what would drift, and the symptom would be a
    tool that survives its plugin on one path and not the other.
    """
    from agent.plugins.assets import PluginAssets

    registry = ToolRegistry()
    catalog = type("_Catalog", (), {"_tool_registry": registry})()
    service = PluginAssets(catalog)

    undos: list = []

    class _Ctx:
        name = "via_assets"

        def on_unwind(self, handler):
            undos.append(handler)

    service.scoped(_Ctx()).add_tool(_Tool("from_assets"))
    assert "from_assets" in registry.list_tools()
    assert registry.tool_source("from_assets") == "plugin:via_assets"

    for undo in undos:
        undo()
    assert "from_assets" not in registry.list_tools()


def test_a_composition_without_a_registry_stays_loadable() -> None:
    """A plugin must not be able to make a minimal composition unloadable."""
    from agent.plugins.assets import PluginAssets

    catalog = type("_Catalog", (), {"_tool_registry": None})()

    class _Ctx:
        name = "p"
        def on_unwind(self, handler): ...

    PluginAssets(catalog).scoped(_Ctx()).add_tool(_Tool("x"))  # must not raise


# ── Commands: the same shape, one registry instead of three dicts ────────────


def _command_host():
    from agent.commands.registry import CommandRegistry

    registry = CommandRegistry()
    host = PluginHost()
    host.services.provide("commands", registry)
    return host, registry


class _CommandPlugin:
    name = "command_plugin"
    inject = ("commands",)

    def __init__(self, *names: str) -> None:
        self._names = names

    def setup(self, ctx):
        for name in self._names:
            ctx.commands.add(name, lambda raw, components: f"{name}:{raw}", metadata={"description": name})


def test_a_plugin_registers_a_command_through_its_context() -> None:
    host, registry = _command_host()
    host.load(_CommandPlugin("deploy"))
    assert registry.has("deploy")
    assert registry.owner("deploy") == "command_plugin"


def test_the_three_facts_live_in_one_record() -> None:
    """The failure the parallel dicts made possible, asserted against.

    Handler, metadata and owner used to be three dicts written in two places.  A key
    present in one and absent from another was representable -- and the conflict
    message read the owner map while the loader wrote the handler, so a command could
    exist with no owner to name.
    """
    host, registry = _command_host()
    host.load(_CommandPlugin("deploy"))

    assert registry.get("deploy") is not None
    assert registry.metadata()["deploy"]["description"] == "deploy"
    assert registry.owner("deploy") == "command_plugin"

    # Removing takes all three, because there is only one thing to remove.
    registry.remove("deploy")
    assert registry.names() == ()
    assert registry.metadata() == {}
    assert registry.owner("deploy") == ""


def test_unloading_the_plugin_removes_its_commands() -> None:
    host, registry = _command_host()
    host.load(_CommandPlugin("deploy", "rollback"))
    assert set(registry.names()) == {"deploy", "rollback"}

    host.unload("command_plugin")
    assert registry.names() == ()


def test_two_plugins_contribute_commands_independently() -> None:
    host, registry = _command_host()

    class _Other:
        name = "other"
        inject = ("commands",)

        def setup(self, ctx):
            ctx.commands.add("other_cmd", lambda raw, components: raw)

    host.load(_CommandPlugin("mine"))
    host.load(_Other())
    host.unload("command_plugin")
    assert registry.names() == ("other_cmd",)


def test_a_snapshot_restores_the_state_it_captured() -> None:
    """What a failed reload relies on.

    Deep enough that an ``add`` after the snapshot cannot mutate it: a shallow copy
    would restore the same record dicts the failed attempt had already written into,
    so the rollback would put back the broken state.
    """
    host, registry = _command_host()
    host.load(_CommandPlugin("deploy"))
    snapshot = registry.snapshot()

    registry.add("deploy", lambda raw, components: "changed", metadata={"description": "new"}, owner="someone")
    registry.add("extra", lambda raw, components: "x")

    registry.restore(snapshot)
    assert registry.names() == ("deploy",)
    assert registry.owner("deploy") == "command_plugin"
    assert registry.metadata()["deploy"]["description"] == "deploy"


def test_the_catalog_can_be_the_loader_for_declarative_commands() -> None:
    """The other writer: the markdown loader returns undos from the same registry."""
    import tempfile
    from pathlib import Path

    from agent.plugins.catalog import PluginCatalog

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        commands = root / "commands"
        commands.mkdir()
        (commands / "review.md").write_text("---\ndescription: reviews\n---\nReview\n", encoding="utf-8")

        catalog = PluginCatalog(builtin_dir=root)
        try:
            undos = catalog._load_command_files(root, "p")
            assert len(undos) == 1
            assert catalog._commands.has("p:review")
            assert catalog._commands.owner("p:review") == "p"

            undos[0]()
            assert not catalog._commands.has("p:review")
        finally:
            catalog.close()


# ── Every contribution is scope-owned, not name-filtered ─────────────────────


def test_unload_takes_every_kind_of_contribution(tmp_path) -> None:
    """Not by filtering lists on a name -- by the scope the contributions recorded on.

    ``_remove_plugin_owned_state`` used to filter skills, MCP configs and agent
    definitions by plugin name, and the slash commands and tools before that.  Those
    filters were removed once each contribution recorded its own removal, and this is
    what says the removal actually covers them: without it, a filter that stopped
    matching and a scope that never recorded anything would look the same, and the
    symptom would be a plugin whose assets outlive it.

    One plugin, all five kinds, one unload.
    """
    import textwrap

    from agent.plugins.catalog import PluginCatalog

    root = tmp_path / "plugins"
    plugin = root / "contrib"
    (plugin / "skills").mkdir(parents=True)
    (plugin / "agents").mkdir()
    (plugin / "agents" / "auditor.md").write_text(
        "---\nname: auditor\n---\nbody\n", encoding="utf-8"
    )
    (plugin / "plugin.json").write_text(
        '{"name": "contrib", "mcpServers": [{"name": "contrib-mcp", "command": "true"}]}',
        encoding="utf-8",
    )
    (plugin / "__init__.py").write_text(textwrap.dedent('''
        class _T:
            name = "contrib_tool"
            description = "a tool"
            parameters = {"type": "object", "properties": {}}
            def fn(self, **kw):
                return "ok"

        def register():
            class P:
                name = "contrib"
                def setup(self, ctx):
                    ctx.assets.add_slash_command("contrib_cmd", lambda **kw: "ok")
                    ctx.assets.add_tool(_T())
            return P()
    '''), encoding="utf-8")

    catalog = PluginCatalog(builtin_dir=root)
    registry = ToolRegistry()
    catalog._tool_registry = registry
    try:
        assert catalog.discover_and_load() == ["contrib"]

        # Everything the plugin contributed is present.
        assert catalog._commands.has("contrib_cmd")
        assert "contrib_tool" in registry.list_tools()
        assert any(name == "plugin:contrib:auditor" for name in catalog._agent_defs)
        assert catalog.get_bundled_skills(), "the skills root was registered"
        assert catalog.get_bundled_mcp(), "the mcp config was registered"

        catalog.unload_plugin("contrib")

        # And every one of them is gone, without anything having filtered by name.
        assert not catalog._commands.has("contrib_cmd"), "the slash command"
        assert "contrib_tool" not in registry.list_tools(), "the tool"
        assert not any(
            name == "plugin:contrib:auditor" for name in catalog._agent_defs
        ), "the agent definition"
        assert not [
            entry for entry in catalog.get_bundled_skills() if entry[0] == "contrib"
        ], "the skills root"
        assert not [
            entry for entry in catalog.get_bundled_mcp() if entry[0] == "contrib"
        ], "the mcp config"
    finally:
        catalog.close()

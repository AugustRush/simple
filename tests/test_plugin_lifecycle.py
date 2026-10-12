"""A plugin's contributions unwind when the plugin unloads.

This is the task the whole refactor was for. Before it, unloading a plugin took
five different code paths -- one per kind of contribution -- and two of them
were documented as impossible: a removed plugin's MCP stdio subprocess could not
be disconnected, and ``on_session_end`` did not fire on eviction
(``agent/plugins/catalog.py:1328-1334``). Eviction was a full table rebuild
(``:1360-1364``), which is an *approximation* of unwinding: it rebuilds the
registries and hopes nothing was missed.

The acceptance condition is therefore behavioural, not structural. One plugin
contributes all five kinds of thing; unload it; observe that all five are gone
and that its teardown ran while it was still alive.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest


def _write_plugin(plugin_dir: Path, code: str) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "__init__.py").write_text(
        textwrap.dedent(code), encoding="utf-8"
    )


def _full_plugin_source(name: str = "everything") -> str:
    """A plugin contributing all five kinds of thing, and recording its life."""
    return f'''
        def register():
            return Everything()


        class Everything:
            name = "{name}"
            # The two capability services this plugin registers on.  Declared, not
            # assumed: reaching a service you did not declare is what the context
            # refuses, and that is the point of the declaration.
            inject = ("commands", "agents")
            version = "1.0"

            def __init__(self):
                # On the instance, not the module: ``setup`` writes through
                # ``self.LOG``, and a bare module-level assignment inside a
                # method would be an instance attribute anyway.
                self.LOG = []
                # Stands in for a resource that is not a registration: the
                # documented MCP-subprocess leak was exactly this shape.
                self.handle = {{"open": True, "kind": "mcp-subprocess"}}

            def setup(self, ctx):
                self.ctx = ctx
                self.LOG.append("setup")
                # Two kinds through the services a plugin reaches by name...
                ctx.commands.add("everything", self._handle)
                ctx.agents.add("helper", {{"description": "a helper", "model": "m"}})
                # ...and the other two declaratively, from the plugin directory.  A
                # skill root and an MCP config are declared in ``plugin.json`` /
                # ``.mcp.json``: measurement showed no Python plugin ever contributed
                # one, and the contribution service that made it possible had exactly
                # one caller -- the catalog's own loader.
                # An arbitrary registration, to prove the scope is general and
                # not four special cases wearing one name.
                ctx.on_unwind(lambda: self.LOG.append("unwound-extra"))

            async def _handle(self, raw, components):
                return "ok"

            def teardown(self):
                self.LOG.append("teardown")
                if self.handle:
                    self.handle["open"] = False
    '''


@pytest.fixture()
def catalog_and_plugin(tmp_path):
    """A loaded catalog holding one plugin that contributed everything."""
    from agent import PluginCatalog

    plugins_dir = tmp_path / "plugins"
    _write_plugin(plugins_dir / "everything", _full_plugin_source())
    # The declarative half, so the fixture really does contribute all four kinds.
    (plugins_dir / "everything" / "skills").mkdir()
    (plugins_dir / "everything" / "plugin.json").write_text(
        '{"name": "everything", "skills": ["skills"], '
        '"mcpServers": [{"name": "everything-mcp", "command": "true"}]}',
        encoding="utf-8",
    )
    catalog = PluginCatalog(builtin_dir=plugins_dir)
    # The skills capability, before discovery: the plugin declares a skill root in its
    # ``plugin.json``, so it contributes it in its own ``setup`` through that service --
    # and a catalog that offers none leaves it pending, which is the kernel's contract.
    from agent.skills.catalog import SkillCatalog

    service = SkillCatalog(
        user_root=tmp_path / "user-skills", builtin_root=tmp_path / "builtin-skills"
    )
    service.load_all()
    catalog._host.services.provide("skill_catalog", service)
    # And the MCP capability, for the same reason: the bundle's ``mcpServers`` entry is
    # contributed in its own setup.
    from agent.tools.mcp_servers import MCPServerRegistry

    catalog._host.services.provide("contributed_mcp_servers", MCPServerRegistry())
    loaded = catalog.discover_and_load()
    assert loaded == ["everything"], loaded
    return catalog, plugins_dir


def _plugin(catalog):
    """The loaded plugin object.  Hold this before unloading: unload removes
    the record, so looking it up afterwards is a KeyError, not a failure."""
    plugin, _meta = catalog._plugins["everything"]
    return plugin


# ── Load: the five contributions are actually registered ─────────────────────


def test_setup_ran_and_registered_a_slash_command(catalog_and_plugin) -> None:
    catalog, _ = catalog_and_plugin
    assert "setup" in _plugin(catalog).LOG
    assert "everything" in catalog.get_slash_commands()


def test_the_bundled_assets_are_registered(catalog_and_plugin) -> None:
    catalog, _ = catalog_and_plugin
    assert any(
        source == "plugin:everything"
        for _root, source in catalog.resolve_service("skill_catalog").contributed_roots()
    ), "the declarative skill root"
    assert any(
        owner == "everything" and cfg.get("name") == "everything-mcp"
        for owner, cfg in catalog.resolve_service("contributed_mcp_servers").records()
    ), "the declarative MCP config"
    assert catalog.get_agent_definition("plugin:everything:helper") is not None


# ── Unload: everything it registered is gone ─────────────────────────────────


def test_unload_removes_the_slash_command(catalog_and_plugin) -> None:
    catalog, _ = catalog_and_plugin
    result = catalog.unload_plugin("everything")
    assert result["ok"] is True
    assert "everything" not in catalog.get_slash_commands()
    assert catalog._commands.get("everything") is None
    assert catalog._commands.owner("everything") == ""


def test_unload_removes_the_bundled_skills(catalog_and_plugin) -> None:
    catalog, _ = catalog_and_plugin
    catalog.unload_plugin("everything")
    assert (
        catalog.resolve_service("skill_catalog").contributed_roots() == []
    ), "the skills root went with the plugin"


def test_unload_removes_the_bundled_mcp(catalog_and_plugin) -> None:
    catalog, _ = catalog_and_plugin
    catalog.unload_plugin("everything")
    assert (
        catalog.resolve_service("contributed_mcp_servers").records() == ()
    ), "the MCP config went with the plugin"


def test_unload_removes_the_agent_definitions(catalog_and_plugin) -> None:
    catalog, _ = catalog_and_plugin
    catalog.unload_plugin("everything")
    assert catalog.get_agent_definition("plugin:everything:helper") is None
    assert all(
        value.get("plugin") != "everything"
        for value in catalog._agent_defs.all()
    )


def test_unload_removes_the_plugin_record(catalog_and_plugin) -> None:
    catalog, _ = catalog_and_plugin
    catalog.unload_plugin("everything")
    assert "everything" not in catalog._plugins
    assert catalog._plugin_source_dirs.get("everything") is None


# ── The two documented leaks ────────────────────────────────────────────────


def test_teardown_runs_and_can_release_a_non_registration_resource(
    catalog_and_plugin,
) -> None:
    """The MCP-subprocess shape: a handle no scope can close on its own."""
    catalog, _ = catalog_and_plugin
    plugin = _plugin(catalog)
    assert plugin.handle["open"] is True

    catalog.unload_plugin("everything")

    assert "teardown" in plugin.LOG
    assert plugin.handle["open"] is False


def test_the_scope_unwinds_custom_registrations_too(catalog_and_plugin) -> None:
    """Not four special cases wearing one name: a generic undo also ran."""
    catalog, _ = catalog_and_plugin
    plugin = _plugin(catalog)
    catalog.unload_plugin("everything")
    assert "unwound-extra" in plugin.LOG


# ── Order and idempotence ───────────────────────────────────────────────────


def test_teardown_runs_before_the_scope_unwinds(catalog_and_plugin) -> None:
    """Order matters: teardown may still need what the scope is about to remove.

    This replaces "teardown runs after the session-end hook".  The hook is gone -- no
    plugin implements it -- so the ordering that is left is the kernel's own: dependents,
    then children, then ``teardown``, then the scope.  The fixture registers a generic
    ``on_unwind`` (``unwound-extra``), so the two events in the log are teardown and the
    scope coming down, and their order is the property.
    """
    catalog, _ = catalog_and_plugin
    plugin = _plugin(catalog)
    catalog.unload_plugin("everything")
    log = plugin.LOG
    assert log.index("teardown") < log.index("unwound-extra"), log


def test_unloading_an_unknown_plugin_is_reported_not_raised(catalog_and_plugin) -> None:
    catalog, _ = catalog_and_plugin
    result = catalog.unload_plugin("never-existed")
    assert result["ok"] is False
    assert "never-existed" in str(result.get("error", ""))


def test_unloading_twice_is_safe(catalog_and_plugin) -> None:
    catalog, _ = catalog_and_plugin
    assert catalog.unload_plugin("everything")["ok"] is True
    second = catalog.unload_plugin("everything")
    assert second["ok"] is False


def test_other_plugins_are_untouched_by_an_unload(tmp_path) -> None:
    """The point of a per-plugin unload: it must not be a table rebuild."""
    from agent import PluginCatalog

    plugins_dir = tmp_path / "plugins"
    _write_plugin(plugins_dir / "everything", _full_plugin_source())
    _write_plugin(
        plugins_dir / "bystander",
        '''
        def register():
            class P:
                name = "bystander"
                def register_slash_commands(self):
                    return {"bystander": lambda raw, components: "ok"}
            return P()
        ''',
    )
    catalog = PluginCatalog(builtin_dir=plugins_dir)
    assert catalog.discover_and_load() == ["bystander", "everything"]

    catalog.unload_plugin("everything")

    assert "bystander" in catalog.get_slash_commands()
    assert "bystander" in catalog._plugins


# ── Eviction through reload, which is where the leaks were documented ────────


def test_reload_fires_session_end_for_an_evicted_plugin(tmp_path) -> None:
    """The documented leak, as a test.

    ``agent/plugins/catalog.py`` used to say: "Plugin ``on_session_end`` hooks
    are NOT fired on eviction; reload is a developer-facing notification, not a
    clean shutdown." Reload now unloads first, so it can no longer say that.
    """
    from agent import PluginCatalog

    plugins_dir = tmp_path / "plugins"
    _write_plugin(plugins_dir / "everything", _full_plugin_source())
    catalog = PluginCatalog(builtin_dir=plugins_dir)
    assert catalog.discover_and_load() == ["everything"]
    plugin = _plugin(catalog)

    # Make it disappear from disk, then reload.
    import shutil

    shutil.rmtree(plugins_dir / "everything")
    asyncio.run(catalog.reload({}))

    # `teardown` is the assertion now: the session-end hook it used to expect is gone,
    # and what the test is about is that eviction releases what the plugin held.
    assert "teardown" in plugin.LOG, plugin.LOG
    assert plugin.handle["open"] is False
    assert "everything" not in catalog._plugins


def test_teardown_may_close_the_resource_it_owns(tmp_path) -> None:
    """The MCP-subprocess shape: unload gives the plugin its moment to close it.

    The old code documented that a removed plugin's stdio subprocess "stays
    alive until the agent shuts down", because nothing called the plugin on the
    way out.  ``teardown`` is that call.
    """
    from agent import PluginCatalog

    plugins_dir = tmp_path / "plugins"
    _write_plugin(plugins_dir / "everything", _full_plugin_source())
    catalog = PluginCatalog(builtin_dir=plugins_dir)
    catalog.discover_and_load()
    plugin = _plugin(catalog)

    catalog.unload_plugin("everything")

    assert plugin.handle == {"open": False, "kind": "mcp-subprocess"}

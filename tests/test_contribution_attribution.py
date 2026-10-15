"""A contribution belongs to the plugin that made it, and to no other.

These pin three cases where the undo reached past its own contribution.  All three
were found by auditing the tree against the pluginization goal rather than by a
failing behaviour: the damage shows up as *another* plugin's capability
disappearing, which reads as "that plugin is flaky" rather than as an attribution
bug.  The general rule they broke is the one the capability services exist to
enforce: attribute a contribution to its contributor, and undo it by identity
rather than by a name or a prefix two contributors can share.
"""

from __future__ import annotations

from pathlib import Path

from agent.kernel.plugin import PluginHost


# ── Tools: a prefix is not an owner ──────────────────────────────────────────


def _contributor(plugin_name: str, tool_name: str) -> object:
    class Contributor:
        name = plugin_name
        inject = ("tools",)

        def setup(self, ctx):
            ctx.tools.register(
                tool_name,
                f"{tool_name} tool",
                {"type": "object", "properties": {}},
                lambda **_: "ok",
            )

    return Contributor()


def test_unloading_one_plugin_keeps_a_prefix_sharing_plugins_tools() -> None:
    """``plugin:foo`` is a prefix of ``plugin:foobar``, so a prefix undo is wrong.

    The undo used to be ``unregister_by_source_prefix("plugin:foo")``, which removed
    every tool whose source *starts with* that string -- including all of the plugin
    literally named ``foobar``.
    """
    from agent.tools.runtime import ToolRegistry

    registry = ToolRegistry()
    host = PluginHost()
    host.services.provide("tools", registry)

    host.load(_contributor("foo", "alpha"))
    host.load(_contributor("foobar", "beta"))
    assert set(registry.list_tools()) == {"alpha", "beta"}

    host.unload("foo")

    assert set(registry.list_tools()) == {"beta"}, (
        "unloading 'foo' must not touch 'foobar's tools"
    )


def test_a_stale_tool_undo_does_not_remove_its_replacement() -> None:
    """The undo is the *definition* it installed, not the name it installed it under.

    ``register`` refuses a different source claiming a name, so the case that matters
    is one contributor replacing its **own** tool -- on a reload, or a second
    registration in one setup.  The older undo is then stale, and running it must be
    a no-op rather than removing the definition that replaced it.
    """
    from agent.tools.runtime import ToolRegistry

    schema = {"type": "object", "properties": {}}
    registry = ToolRegistry()
    first = registry.register("shared", "v1", schema, lambda **_: "v1", source="plugin:x")
    second = registry.register(
        "shared", "v2", schema, lambda **_: "v2", source="plugin:x", replace=True
    )

    registry.unregister_contribution("shared", first)

    assert "shared" in registry.list_tools(), (
        "a stale undo removed the tool that replaced it"
    )
    assert registry.tool_source("shared") == "plugin:x"

    registry.unregister_contribution("shared", second)

    assert "shared" not in registry.list_tools()


# ── Commands: a name is not an owner either ──────────────────────────────────


def test_a_command_undo_does_not_remove_another_owners_command() -> None:
    """A registers ``x``, B overwrites ``x``, A unloads -- B's command must survive."""
    from agent.commands.registry import CommandRegistry

    registry = CommandRegistry()
    undo_first = registry.add("x", lambda: "first", owner="first")
    registry.add("x", lambda: "second", owner="second")

    undo_first()

    assert registry.has("x"), "the first owner's undo removed the second's command"
    assert registry.owner("x") == "second"
    assert registry.get("x")() == "second"


def test_a_command_undo_still_removes_its_own_record() -> None:
    """Control: identity-scoped does not mean inert."""
    from agent.commands.registry import CommandRegistry

    registry = CommandRegistry()
    undo = registry.add("x", lambda: "only", owner="only")
    undo()

    assert not registry.has("x")


# ── Skills: equality is not identity ────────────────────────────────────────


def test_two_equal_skill_roots_detach_independently(tmp_path: Path) -> None:
    """Two contributors may attach records that compare equal.

    ``add_root``'s undo used ``record in self._roots`` + ``remove(record)`` -- equality,
    which takes the *first* equal record.  With two identical attachments, the second
    contributor's unload detached the first contributor's root.
    """
    from agent.skills.catalog import SkillCatalog

    root = tmp_path / "shared-skills"
    root.mkdir()
    (root / "one.md").write_text("# one\n", encoding="utf-8")

    catalog = SkillCatalog()
    first = catalog.add_root(root, source="first")
    second = catalog.add_root(root, source="second")

    assert sorted(contributed_sources(catalog)) == ["first", "second"]

    second()

    assert contributed_sources(catalog) == ["first"], (
        "the second contribution's undo detached the first contribution's root"
    )

    first()

    assert contributed_sources(catalog) == []


def contributed_sources(catalog) -> list[str]:
    return [source for _root, source in catalog.contributed_roots()]

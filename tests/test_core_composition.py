"""The composition's own plugins are data, so a composition can say what it has.

Six plugins are mounted by the composition root rather than discovered on disk.  They were
five literal ``mount(...)`` calls in a 900-line function, so a question a composition model
should answer -- "this deployment has no memory plugin" -- had **no way to be asked**.  A
profile could narrow the *discovered* plugins and could not touch these at all.

This was only possible because of a measurement: **all 24 orders** of memory / session /
agent / tools produce the same complete composition.  A list that can be reordered freely is a
list that can become data.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from agent.plugins.core import CORE_PLUGINS, CoreContext, core_plugin_names, core_plugins

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_the_declaration_names_every_core_plugin() -> None:
    """One list, and the composition root mounts from it rather than beside it."""
    assert core_plugin_names() == (
        "ralph",
        "memory",
        "sqlite-fts-memory",
        "session",
        "agent",
        "tools",
        "agent_loop",
    )


def test_a_selection_narrows_the_set_and_says_so() -> None:
    """"This deployment has no memory plugin" is now askable.

    Order is preserved from the declaration rather than from the selection, so a caller
    cannot accidentally reorder the composition by listing names differently.
    """
    chosen = core_plugins(enabled=("tools", "memory"))

    assert [plugin.name for plugin in chosen] == ["memory", "tools"]


def test_no_selection_means_all_of_them() -> None:
    """The safe default: a deployment that says nothing keeps what it had.

    "All of them" is every plugin that is **on by default**.  A plugin marked
    ``opt_in`` exists to replace a default one -- mounting both would be two
    providers of one name, which the kernel refuses -- so the default set is the
    declaration minus the opt-ins, and that is what this compares.
    """
    default = [plugin for plugin in CORE_PLUGINS if not plugin.opt_in]
    assert list(core_plugins()) == default
    assert list(core_plugins(enabled=None)) == default
    assert "sqlite-fts-memory" in core_plugin_names(), "declared"
    assert "sqlite-fts-memory" not in [plugin.name for plugin in core_plugins()], "not mounted"


def test_an_unknown_name_is_refused_rather_than_ignored() -> None:
    """A composition asking for a plugin that does not exist is a mistake, not a no-op.

    Silently dropping it would produce a composition that is missing something the operator
    asked for, which is the failure this whole arrangement is meant to make visible.
    """
    with pytest.raises(KeyError) as error:
        core_plugins(enabled=("memory", "nonexistent"))

    assert "nonexistent" in str(error.value)


def test_the_composition_root_mounts_from_the_declaration() -> None:
    """Asserted on the source, because the failure mode is a literal `mount` reappearing.

    A new plugin mounted beside the declaration would work and would be invisible to every
    profile -- which is exactly how the six ended up uncountable in the first place.
    """
    source = (REPO_ROOT / "agent/bootstrap.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_build_components_async"
    )

    literal_mounts = []
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "mount" or not node.args:
            continue
        argument = node.args[0]
        # `plugin_catalog.mount(<Name>())` is the literal form; the declaration loop passes
        # `_core_plugin.build(...)`.
        if isinstance(argument, ast.Call) and isinstance(argument.func, ast.Name):
            literal_mounts.append(f"L{node.lineno}: {argument.func.id}")

    assert not literal_mounts, (
        "the composition root must mount core plugins from the declaration, not by naming "
        f"classes: {literal_mounts}"
    )
    assert "core_plugins(" in source


def test_the_factory_is_handed_what_it_needs_rather_than_the_mapping() -> None:
    """A factory that can reach anything makes "what does this need" unanswerable.

    ``CoreContext`` carries the two classes the composition substitutes and nothing else --
    the same narrowing that ``BaseAgent``'s ``services`` mapping got, for the same reason.
    """
    import inspect

    fields = set(inspect.signature(CoreContext).parameters)
    assert fields == {"agent_class", "tools_class", "extra"}, fields

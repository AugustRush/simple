"""A profile can say a *service* comes from somewhere else.

`core` says which of the composition's own plugins are present.  It cannot say that a
particular **service** is provided by a different plugin -- and a deployment shipping its own
memory plugin needs exactly that: "include memory" and "memory comes from acme" are different
statements.

The mechanisms already existed: a plugin of the same name replaces through
``load(replace=True)``, and the dependents are restored afterwards.  What was missing is the
**declaration**, and the **check** -- whether the named plugin actually came up and actually
provided the service is a different question from what the profile asked for.
"""

from __future__ import annotations

import pytest

from agent.plugins.core import CORE_PLUGINS, core_plugins


def test_a_replacement_drops_the_core_plugin_that_would_have_provided_it() -> None:
    """Two providers of one name is a collision the kernel refuses at the second `provide`.

    That error names neither the profile nor the replacement, so the decision is made here
    instead -- by not mounting the plugin whose slot was claimed.
    """
    chosen = core_plugins(replacements={"ltm_store": "acme.memory"})

    names = [plugin.name for plugin in chosen]
    assert "memory" not in names, "the slot was claimed by a replacement"
    assert "session" in names, "only the plugin providing the replaced service is dropped"


def test_a_replacement_of_an_unrelated_service_changes_nothing() -> None:
    """Control: a service no core plugin provides displaces nothing.

    Compared against the *default* set rather than ``CORE_PLUGINS``: the
    declaration also holds opt-in plugins (an alternative memory backend), which
    nothing mounts until something asks for them.
    """
    assert core_plugins(replacements={"something_else": "acme.thing"}) == core_plugins()


def test_every_core_plugin_that_provides_the_service_is_dropped() -> None:
    """By `provides`, not by name: a plugin may offer more than the slot it is known for.

    `memory` provides both `ltm_store` and `memory`, so replacing either must drop it -- a
    name-based rule would drop it for one spelling and leave a collision for the other.
    """
    by_storage = [p.name for p in core_plugins(replacements={"ltm_store": "x"})]
    by_palace = [p.name for p in core_plugins(replacements={"memory": "x"})]

    assert "memory" not in by_storage
    assert "memory" not in by_palace


def test_the_declaration_is_a_mapping_of_service_to_plugin() -> None:
    """Named for what it is: the *service* is what a profile reasons about.

    A map keyed by core-plugin name would be the same information spelled in a way that
    cannot express "the service, not the plugin" -- which is the whole distinction between
    this and `core`.
    """
    from agent.plugins.profiles import PluginProfile

    profile = PluginProfile(
        key="acme",
        label="Acme",
        summary="our memory",
        service_replacements={"ltm_store": "acme.memory", "memory": "acme.memory"},
    )

    assert profile.service_replacements == {
        "ltm_store": "acme.memory",
        "memory": "acme.memory",
    }
    assert profile.to_payload()["service_replacements"] == {
        "ltm_store": "acme.memory",
        "memory": "acme.memory",
    }


def _calls_in(source: str, function: str, *, asynchronous: bool = False) -> set[str]:
    """Attribute names called inside *function*'s body."""
    import ast

    tree = ast.parse(source)
    wanted = ast.AsyncFunctionDef if asynchronous else ast.FunctionDef
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, wanted) and node.name == function
    )
    return {
        node.func.attr
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def test_the_composition_checks_the_replacement_rather_than_trusting_it() -> None:
    """Deciding what not to mount does not tell you whether the replacement arrived.

    Without the check the failure is a `KeyError` three layers away naming neither the profile
    nor the replacement -- so the composition asks who actually provided each declared service
    and refuses to come up if the answer is not the declared plugin.

    The check lives on the *catalog* rather than inline in the composition root, because a
    recomposition has to run the same one after it rebuilds (``PluginCatalog.recompose``);
    two copies of a check is one copy that drifts.  What this asserts is the chain: the root
    calls it, and it asks the questions that make it a check.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "agent"
    bootstrap = (root / "bootstrap.py").read_text(encoding="utf-8")
    catalog_source = (root / "plugins/catalog.py").read_text(encoding="utf-8")

    assert "verify_replacements" in _calls_in(
        bootstrap, "_build_components_async", asynchronous=True
    ), "the composition root must run the check"

    calls = _calls_in(catalog_source, "verify_replacements")
    assert "who_provides" in calls, "the check must ask who provided it"
    assert "service_replacements" in calls, "and must read the declaration"


def test_a_failed_replacement_says_which_plugin_was_expected() -> None:
    """The error is the feature: 'declared X, but it is Y' is actionable; a KeyError is not."""
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "agent/plugins/catalog.py"
    ).read_text(encoding="utf-8")

    assert "declares" in source and "not provided at all" in source

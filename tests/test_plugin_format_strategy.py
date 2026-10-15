"""An ecosystem's *shapes* are the format's, not the loader's.

``PluginFormat`` used to describe only where an ecosystem keeps its files, while what
those files *mean* -- matcher groups around command hooks, ``$ARGUMENTS`` in a command
body, the hook output contract -- lived in the loader and was applied to every
ecosystem.  So "one ecosystem = one adapter" held for paths and not for shapes: an
ecosystem whose files meant something else needed a branch in the loader.

These tests hold the seam open.  The first two are the property (a format's strategy
is what runs); the third is the absence that makes the first two worth having (the
loader no longer knows the shapes); the fourth is the default every format inherits.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from agent.plugins import formats
from agent.plugins.formats import PluginFormat


def _temporary_format(**overrides) -> PluginFormat:
    """A format that claims ``acme.json``, so a tmp dir resolves to it."""
    return PluginFormat(
        key="acme-strategy-test",
        label="Acme (strategy test)",
        manifests=("acme.json",),
        **overrides,
    )


def test_a_format_supplies_its_own_hook_normalization(tmp_path: Path) -> None:
    """The seam: replace the strategy, and the loader's behaviour changes with it."""
    from agent.plugins.catalog import _merge_hooks_block

    (tmp_path / "acme.json").write_text("{}", encoding="utf-8")
    seen: list[tuple[str, str, object]] = []

    def shout(plugin_name: str, event_name: str, entries: object):
        seen.append((plugin_name, event_name, entries))
        return "on_shouted", [{"type": "command", "command": "echo shout"}]

    fmt = _temporary_format(normalize_hook_entries=shout)
    formats.register_format(fmt, position=0)
    try:
        into: dict[str, list[dict]] = {}
        _merge_hooks_block(tmp_path, "acme", {"SomeEvent": [{"command": "x"}]}, into)
    finally:
        formats.PLUGIN_FORMATS.remove(fmt)

    assert seen == [("acme", "SomeEvent", [{"command": "x"}])], (
        "the loader must ask the format, not translate the shape itself"
    )
    assert list(into) == ["on_shouted"]
    assert into["on_shouted"][0]["command"] == "echo shout"


def test_a_format_supplies_its_own_command_arguments() -> None:
    """``$ARGUMENTS`` is one ecosystem's convention, so it is that format's strategy."""
    from agent.plugins.catalog import _make_markdown_command_handler

    fmt = _temporary_format(
        substitute_command_args=lambda body, args: f"<{args}>{body}"
    )
    handler = _make_markdown_command_handler(
        strategy=fmt,
        plugin_name="acme",
        cmd_key="acme:run",
        body="the body",
        description="",
    )

    import asyncio

    assert asyncio.run(handler("acme:run now", {})) == "<now>the body"


def test_a_format_supplies_its_own_hook_output_translation() -> None:
    from agent.plugins.formats import default_translate_hook_output

    fmt = _temporary_format(translate_hook_output=lambda raw, event: {"blocked": True})
    assert fmt.translate_hook_output({"x": 1}, "on_pre_tool") == {"blocked": True}
    # And the default still reads the shared contract, so nothing regressed for the
    # formats that do not override it.
    assert default_translate_hook_output({"continue": False}, "on_pre_tool").action == "block"


def test_every_strategy_has_a_default() -> None:
    """A field with no default would make every existing format incomplete."""
    bare = PluginFormat(key="bare", label="Bare")

    assert callable(bare.normalize_hook_entries)
    assert callable(bare.translate_hook_output)
    assert callable(bare.substitute_command_args)
    internal, flat = bare.normalize_hook_entries("p", "PreToolUse", [{"command": "x"}])
    assert internal == "on_pre_tool", "the merged vocabulary still translates names"
    assert flat[0]["command"] == "x"


def test_the_loader_keeps_no_ecosystem_shape_knowledge() -> None:
    """The absence that makes the seam matter.

    Asserted on the source because the property *is* an absence: the loader must not
    contain the conventions.  Each string below was a literal in ``catalog.py``
    before the strategies moved; if one comes back, the branch that reads it came
    back with it.
    """
    source = (Path(__file__).resolve().parents[1] / "agent/plugins/catalog.py").read_text(
        encoding="utf-8"
    )
    banned = {
        "matcher group flattening": 'nested = entry.get("hooks")',
        "hook output contract": 'raw.get("hookSpecificOutput")',
        "command argument convention": 'body.replace("$ARGUMENTS"',
        "permission decision vocabulary": 'specific.get("permissionDecision")',
    }
    offenders = [name for name, literal in banned.items() if literal in source]
    assert not offenders, (
        "the loader knows an ecosystem's shapes again: "
        + ", ".join(offenders)
        + " -- move it onto PluginFormat as a strategy"
    )


def test_the_moved_strategies_are_exported_and_documented() -> None:
    """A strategy a format may override has to be reachable and named in __all__."""
    for name in formats.__all__:
        assert hasattr(formats, name), f"{name} is exported but missing"
    for name in (
        "default_normalize_hook_entries",
        "default_translate_hook_output",
        "default_substitute_command_args",
        "strategy_for",
    ):
        assert name in formats.__all__, f"{name} must be importable by a format author"
        assert inspect.getdoc(getattr(formats, name)), f"{name} has no docstring"


def test_strategy_for_falls_back_to_the_native_format(tmp_path: Path) -> None:
    """A directory no ecosystem claimed is handled by this project's own conventions."""
    assert formats.strategy_for(tmp_path) is formats.NATIVE
    assert formats.strategy_for(None) is formats.NATIVE

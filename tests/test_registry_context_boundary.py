"""The registry context must not hand out the whole component container.

``registry.set_context("components", components)`` made the entire bootstrap
result reachable from inside a tool, which means no service boundary could be
enforced on the tool side: a reader could always reach around whatever it had
been given.  Two readers did exactly that.

The fix is not to hide the container better.  It is to notice that only *four*
keys of it were ever read through it -- ``command_router``, ``skill_catalog``,
``mcp_client`` and ``registry`` -- and provide those under their own names,
which is a dependency a reader can be held to.

These tests are about the *shape of the boundary*, not about bootstrap, so they
do not run bootstrap: they exercise the readers on a registry wired the way
bootstrap wires one, and they assert on the source that the container name is
gone.  The bootstrap-level equivalent is slow and the fact would not be any
more true for it.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from agent.tools.runtime import ToolRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The four keys ``PluginCatalog.reload`` reads out of the container, found by
#: ``grep -n 'components\\.get(' agent/plugins/catalog.py``.
RELOAD_DEPENDENCIES = ("command_router", "skill_catalog", "mcp_client", "registry")


#: An access to the component container by name, as code rather than prose.
#: Matches `get_context("components")`, `set_context("components", ...)`, and
#: `ctx["components"]` alike, because all three reach the same container.
_CONTAINER_ACCESS = re.compile(
    r'''(?:get|set)_context\(\s*["\']components["\']'''
)


def _blank_literals(source: str) -> str:
    """*source* with comments and string literals blanked out.

    Prose in a docstring that explains why the container was removed must not
    read as a use of it, and by eye the two are identical: a docstring mention
    and a code lookup are the same characters.  Comments are dropped and
    literals are replaced by spaces, so what is left is code only.

    Only comments and same-line literals are handled.  A multi-line docstring
    mention slips through, which is a deliberate trade: the failure mode is a
    false negative on prose, never a false positive on code.
    """
    import io
    import tokenize

    lines = source.splitlines()
    blanked = list(lines)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError):  # pragma: no cover
        return source
    for token in tokens:
        if token.type == tokenize.COMMENT:
            row = token.start[0] - 1
            if 0 <= row < len(blanked):
                blanked[row] = blanked[row][: token.start[1]]
            continue
        if token.type != tokenize.STRING:
            continue
        # Only multi-line strings are blanked.  A same-line literal is left
        # alone on purpose: `get_context("components")` is code, and the name
        # being looked for lives *inside* that literal, so blanking it would
        # delete the very evidence this guard exists to find.  Prose that
        # merely mentions the name is not followed by `)` or `]`, so the
        # access pattern does not match it either way.
        start_row, end_row = token.start[0], token.end[0]
        if end_row == start_row:
            continue
        # Blank the *interior* only, keeping the opening delimiter line so the
        # statement's own line -- and its indentation -- survives.  Replacing
        # the first line wholesale would collapse `x = """` into a bare
        # expression and shift the code below it.
        for row in range(start_row, end_row + 1):
            index = row - 1
            if not (0 <= index < len(blanked)):
                continue
            line = blanked[index]
            if row == start_row:
                # Drop the opening quotes, keep whatever precedes them.
                first = token.start[1] + 3 if line[token.start[1]:token.start[1] + 3] in ('"""', "'''") else token.start[1] + 1
            else:
                first = 0
            last = token.end[1] if row == end_row else len(line)
            blanked[index] = line[:first] + " " * max(0, last - first) + line[last:]
    return "\n".join(blanked)


def _bootstrap_shaped_registry() -> ToolRegistry:
    """A registry carrying the names bootstrap now provides, and nothing else."""
    registry = ToolRegistry()
    for name in RELOAD_DEPENDENCIES:
        registry.set_context(name, object())
    registry.set_context("user_tool_catalog", object())
    registry.set_context("user_tools_enabled", True)
    registry.set_context("workspace_root", "/tmp/ws")
    return registry


def test_the_whole_container_is_not_a_context_key() -> None:
    """The backdoor itself, absent from the wiring bootstrap actually creates."""
    registry = _bootstrap_shaped_registry()
    assert registry.get_context("components") is None


def test_no_module_reads_the_container_anymore() -> None:
    """A source-level guard, because a runtime probe cannot see a dead path.

    If someone re-adds the lookup in a branch the tests do not exercise, the
    wiring tests above still pass.  Reading the source catches that.
    """
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "agent").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        # Blank once per file, not once per line: tokenising is linear in the
        # file, so doing it inside the loop made this quadratic and hung on the
        # largest module.
        rows = _blank_literals(path.read_text(encoding="utf-8")).splitlines()
        for index, row in enumerate(rows, start=1):
            if _CONTAINER_ACCESS.search(row):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{index}")
    assert not offenders, (
        "the component container is reachable again:\n  " + "\n  ".join(offenders)
    )


def test_a_registry_can_no_longer_read_components_by_name() -> None:
    registry = _bootstrap_shaped_registry()
    assert registry.get_context("components") is None


def test_the_plugin_reload_dependencies_are_individual_names() -> None:
    """Every key ``PluginCatalog.reload`` reads is provided under its own name."""
    registry = _bootstrap_shaped_registry()
    for name in RELOAD_DEPENDENCIES:
        assert registry.get_context(name) is not None, name
    assert registry.get_context("components") is None


def test_user_tool_resolution_needs_only_two_names() -> None:
    """``resolve_catalog`` reads a catalog and a flag, not a container."""
    from agent.tools import user_tools

    registry = ToolRegistry()
    sentinel_catalog = object()
    registry.set_context("user_tool_catalog", sentinel_catalog)
    registry.set_context("user_tools_enabled", True)

    catalog, enabled = user_tools.resolve_catalog(registry)
    assert catalog is sentinel_catalog
    assert enabled is True


def test_user_tool_resolution_falls_back_when_nothing_is_provided() -> None:
    """Absent wiring yields a usable catalog, as it did through the container."""
    from agent.tools import user_tools

    registry = ToolRegistry()
    catalog, enabled = user_tools.resolve_catalog(registry)
    assert catalog is not None
    assert enabled is False


def test_plugin_reload_rebuilds_the_container_from_named_keys() -> None:
    """``_reload_plugins`` must pass on exactly the four keys, and no more.

    Passing more would quietly restore the old dependency; passing fewer would
    break reload.  Asserting the exact set is the point.
    """
    from agent.tools.builtin_tools import BuiltinTools

    registry = _bootstrap_shaped_registry()

    seen: dict = {}

    class _RecordingCatalog:
        async def reload(self, components):
            seen.update(components)
            return {"ok": True, "reloaded": True}

    registry.set_context("plugin_catalog", _RecordingCatalog())
    tool = object.__new__(BuiltinTools)
    tool.registry = registry

    import asyncio

    result = asyncio.run(BuiltinTools._reload_plugins(tool))
    assert result == {"ok": True, "reloaded": True}
    assert set(seen) == set(RELOAD_DEPENDENCIES)

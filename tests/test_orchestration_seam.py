"""Why Orchestration is not a composition-level service, pinned so the claim can be checked.

The objective lists Orchestration among the things to turn into a plugin.  It is not one,
and the reason is not size or boundaries -- it is that **its input is per-call state**.

``plan_orchestration`` builds an ``OrchestrationPlanner`` on every turn from
``ctx.metadata["skill_catalog"]``, and that key is written by four different driving paths:
the runtime's per-turn context, Ralph's loop, the CLI loop, and a channel session.  A
composition-time ``orchestration`` service would be built against one of them, and the
other three would be silently ignored -- a behaviour change, not a refactor.

These tests exist so that the claim is falsifiable.  If somebody later lifts the planner to
composition time, one of them has to be edited deliberately, and the edit is where the
question "what did the other three paths lose" gets asked.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _turn_loop_source() -> str:
    return (REPO_ROOT / "agent/core/turn_loop.py").read_text(encoding="utf-8")


def test_the_planner_is_built_per_call_from_the_call_s_own_metadata() -> None:
    """Not a service: the catalog comes from the context being served."""
    source = _turn_loop_source()
    tree = ast.parse(source)
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "plan_orchestration"
    )
    reads = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "metadata"
        and any(
            isinstance(arg, ast.Constant) and arg.value == "skill_catalog"
            for arg in node.args
        )
    ]
    assert reads, (
        "plan_orchestration must read the call's metadata; if the planner no longer does, "
        "this measurement is out of date and the reason Orchestration is not a service "
        "has to be re-established"
    )
    assert "OrchestrationPlanner.from_skill_catalog" in source


def test_more_than_one_path_writes_that_metadata_key() -> None:
    """Which is what makes a composition-time service wrong rather than merely different.

    One writer would mean the planner could be built once for it.  Four writers, each on a
    different driving path, mean any single choice silently drops the others.
    """
    writers = []
    for path in sorted((REPO_ROOT / "agent").rglob("*.py")):
        source = path.read_text(encoding="utf-8", errors="ignore")
        if 'metadata["skill_catalog"]' in source:
            writers.append(path.relative_to(REPO_ROOT).as_posix())
    assert len(writers) >= 4, (
        f"expected the four driving paths to set the catalog; found {writers}"
    )
    assert "agent/core/turn_loop.py" not in writers, (
        "the loop must *read* the catalog, not set it -- setting it there would make the "
        "planner's input look like loop state rather than the caller's"
    )


def test_it_is_called_from_inside_the_loop() -> None:
    """Per turn, not per composition: the call sits in the turn body."""
    source = _turn_loop_source()
    tree = ast.parse(source)
    callers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "plan_orchestration"
    ]
    assert callers, "the scan must find the call"
    assert len(callers) == 1

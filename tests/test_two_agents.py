"""There are two agents, on purpose, and this pins the difference.

`AgentPlugin` provides the process-wide agent.  A **session** builds its own -- inside
`_build_web_session_components`, from the config on disk -- because a session may be rebuilt
after a provider switch, and that is what a rebuild is for: the session's model, client and
token budget all come from the config *now*, not from the one the process started with.

Both write the name `agent`, which is why the ledger records two owners.  The distinction
matters enough to assert, because the failure mode of getting it wrong is silent: a session
that inherited the process agent would answer with the wrong provider's model, and the
symptom is a 400 from the provider rather than anything locally visible.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_the_session_agent_is_built_by_the_session_not_taken_from_the_plugin() -> None:
    """Structural, because the two constructions live in one function and must stay two.

    Counted rather than pattern-matched: the claim is "these are two separate
    constructions", and a scan that found one of them would still pass a `in source` check.
    """
    source = (REPO_ROOT / "agent/bootstrap.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    constructions = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == "BaseAgent":
            constructions.append(node.lineno)
    assert len(constructions) == 1, (
        "one direct construction should remain -- the session's; the process-wide agent is "
        f"provided by AgentPlugin.  Found at lines {constructions}"
    )

    # And that one is inside the session builder, not the process builder.
    session_fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_build_web_session_components"
    )
    assert session_fn.lineno < constructions[0] <= session_fn.end_lineno, (
        f"the remaining construction at L{constructions[0]} is not in the session builder"
    )


def test_the_plugin_is_handed_the_class_and_the_session_is_not() -> None:
    """The two paths use different substitution points, and both are deliberate.

    The plugin takes the class as an argument so the composition can replace it; the
    session builds against `bootstrap`'s own name for the same reason.
    """
    source = (REPO_ROOT / "agent/bootstrap.py").read_text(encoding="utf-8")
    assert "AgentPlugin(BaseAgent)" in source, (
        "the plugin must be handed the module-level name, or replacing bootstrap.BaseAgent "
        "stops deciding which agent runs"
    )
    assert '"agent": agent,' in source, (
        "the session mapping must carry the session's own agent"
    )


def test_a_session_without_a_global_agent_still_gets_one() -> None:
    """Control for the direction of the dependency.

    The session does not *need* the plugin's agent -- it builds one.  If it did need it, the
    construction would be a lookup, and the two would not be two.
    """
    source = (REPO_ROOT / "agent/bootstrap.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    session_fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_build_web_session_components"
    )
    # Read the *constant*, not `ast.unparse(node)`: unparse re-quotes string literals as
    # single quotes, so a `'"agent"' in unparsed` check is silently always false.  That is
    # how this test passed while doing nothing, found by a probe that should have tripped
    # it and did not.
    resolved = []
    for node in ast.walk(session_fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "require_service":
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and arg.value == "agent":
                resolved.append(node.lineno)
    assert not resolved, (
        "the session must build its agent, not resolve the plugin's "
        f"(require_service(..., 'agent') at lines {resolved})"
    )

"""Every declared service is provided by a plugin, or the debt is written down.

The objective is "every module is a plugin".  The mechanism for that has existed
since the kernel landed -- a plugin declares ``provide``/``inject``, its
registrations unwind with it, and a profile can say a service comes from a
different plugin.  What was missing is a *count and a brake*: five of the
eleven declared services are provided through the kernel by a plugin module, and
the other six are still constructed by ``agent/bootstrap.py``.

This test does not move them.  It makes adding a seventh a deliberate act with a
stated reason, and it fails when a waived service becomes plugin-provided -- so
the waiver list cannot rot into a list of things that were true once.

What counts as "provided by a plugin": a ``ctx.provide("<name>", ...)`` call.
That is the kernel's own vocabulary, and it is what makes the service scoped,
reversible and replaceable.  An assignment into the component mapping by the
composition root is the thing this test exists to notice.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent.runtime.core_services import CORE_SERVICES

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = REPO_ROOT / "agent"

#: Services the composition root still assembles, with the reason and what it
#: would take to move each one behind a plugin.  Empty is the goal.
ROOT_ASSEMBLED: dict[str, str] = {
}


#: Services that cannot be provided by a plugin at all, with the reason.  A
#: floor is not debt: the loader is mounted *by* the container, so the container
#: cannot get it from a plugin it has not loaded yet.
FLOORS: dict[str, str] = {
    "agent_core": (
        "A facade over the composition itself: it resolves `agent_loop`, "
        "`skill_catalog`, `plugin_catalog`, `post_turn_maintenance` and `output_dir` at "
        "call time through the container, and it publishes the system prompt by handing "
        "the whole mapping to the publisher. A plugin cannot be handed the container "
        "without making \"which services does this plugin need\" unanswerable, which is "
        "the property the plugin model rests on -- so this is a floor rather than a "
        "service waiting to be moved."
    ),
    "plugin_catalog": (
        "The loader cannot be provided by the thing it loads: the container mounts "
        "plugins through this service, so it has to exist before the first plugin "
        "runs. A floor by construction, not a service waiting to be moved."
    ),
}


def _plugin_providers() -> dict[str, set[str]]:
    """Service name -> modules calling ``ctx.provide("<name>", ...)``."""
    providers: dict[str, set[str]] = {}
    for source in sorted(AGENT_ROOT.rglob("*.py")):
        if "__pycache__" in str(source):
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "provide":
                continue
            if not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                providers.setdefault(first.value, set()).add(
                    str(source.relative_to(REPO_ROOT))
                )
    return providers


def test_the_scan_finds_the_plugin_providers() -> None:
    """Control: the scan reads the kernel's own call, so it must find the known ones."""
    providers = _plugin_providers()

    assert "ltm_store" in providers, "memory plugin provides it"
    assert providers["ltm_store"] == {
        "agent/memory/plugin.py",
        # The opt-in alternative backend provides the same service; naming it as a
        # replacement is what mounts it.
        "agent/memory/backends/sqlite_fts.py",
    }
    assert "context_manager" in providers
    assert "agent_loop" in providers, "the turn runner provides itself"


def test_every_service_is_plugin_provided_or_waived() -> None:
    providers = _plugin_providers()
    plugin_provided = sorted(name for name in providers if name in {
        entry.name for entry in CORE_SERVICES
    })
    root_assembled = sorted(
        entry.name for entry in CORE_SERVICES if entry.name not in plugin_provided
    )

    unwaived = sorted(
        name
        for name in root_assembled
        if name not in ROOT_ASSEMBLED and name not in FLOORS
    )
    assert not unwaived, (
        f"{len(plugin_provided)} of {len(CORE_SERVICES)} services are provided by a "
        f"plugin ({', '.join(plugin_provided)}). These are assembled by the "
        f"composition root with no recorded reason:\n  "
        + "\n  ".join(unwaived)
        + "\n\nProvide it from a plugin, or add it to ROOT_ASSEMBLED in this file "
        "with the reason and what moving it would take."
    )


def test_the_waiver_list_has_no_stale_entries() -> None:
    """A waiver for something that moved is a lie about the current state."""
    providers = _plugin_providers()

    moved = sorted(name for name in ROOT_ASSEMBLED if name in providers)
    assert not moved, (
        "these are waived as root-assembled but a plugin provides them now -- "
        f"remove them from ROOT_ASSEMBLED: {moved}"
    )

    unknown = sorted(
        name
        for name in list(ROOT_ASSEMBLED) + list(FLOORS)
        if name not in {entry.name for entry in CORE_SERVICES}
    )
    assert not unknown, f"waived but not a declared service: {unknown}"

    both = sorted(set(ROOT_ASSEMBLED) & set(FLOORS))
    assert not both, f"waived twice, as moveable and as a floor: {both}"


@pytest.mark.parametrize("name", sorted(FLOORS))
def test_a_floor_says_why_it_cannot_move(name: str) -> None:
    reason = FLOORS[name]

    assert len(reason) > 80, f"{name}: the reason is too short to be one"
    assert "cannot" in reason, (
        f"{name}: a floor has to say why a plugin cannot provide it, not that it "
        "has not been moved yet"
    )


@pytest.mark.parametrize("name", sorted(ROOT_ASSEMBLED))
def test_a_waiver_says_what_moving_it_would_take(name: str) -> None:
    """A waiver without a convergence target is a shrug, not a decision."""
    reason = ROOT_ASSEMBLED[name]

    assert len(reason) > 80, f"{name}: the reason is too short to be one"
    assert "plugin" in reason or "service" in reason, (
        f"{name}: the waiver should say what the plugin-shaped version looks like"
    )

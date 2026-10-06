"""The scheduler's store is constructed in one place.

This is the P4-class debt for the scheduler: `SchedulerStore` was constructed in
six modules, and every one of them resolved `shared.SCHEDULER_DB_FILE` itself --
a module-level global that ``_set_agent_home`` rewrites.  Six readers of one
mutable global is the same shape the ``components`` backdoor had, and the same
fix applies: name the construction once, and give it an explicit parameter.

What this does **not** decide
-----------------------------
``SchedulerService._store_call`` still tests ``type(self.store) is
SchedulerStore`` to decide whether to open a thread-owned SQLite connection.
That is a real trade-off about thread affinity, and it is deliberately left
alone here -- see the plan's Task 10 note.  This module is about *where the store
comes from*, not about what the scheduler may assume once it has one.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The only package allowed to name the concrete class in a call.
_STORE_PACKAGE = "agent/scheduler"


def _constructions(source: str) -> int:
    """Count ``SchedulerStore(...)`` calls, ignoring prose and definitions."""
    tree = ast.parse(source)
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name == "SchedulerStore":
            count += 1
    return count


# ── The seam ─────────────────────────────────────────────────────────────────


def test_open_store_is_the_named_constructor() -> None:
    from agent.scheduler import open_store

    assert callable(open_store)
    assert open_store.__module__.startswith("agent.scheduler")


def test_open_store_returns_a_store(tmp_path: Path) -> None:
    from agent.scheduler import open_store
    from agent.scheduler.store import SchedulerStore

    store = open_store(tmp_path / "scheduler.db")
    try:
        assert isinstance(store, SchedulerStore)
        assert Path(store.db_path) == tmp_path / "scheduler.db"
    finally:
        store.close()


def test_open_store_accepts_an_explicit_path(tmp_path: Path) -> None:
    """The parameter is the point: a caller may point at another database.

    Without it, every caller resolved a module-level global, which is what made
    the path a property of the process rather than of the caller.
    """
    from agent.scheduler import open_store

    target = tmp_path / "other.db"
    store = open_store(target)
    try:
        assert Path(store.db_path) == target
    finally:
        store.close()


def test_open_store_without_a_path_uses_the_configured_default() -> None:
    """``None`` resolves through the store's own default, not through a caller."""
    from agent import shared
    from agent.scheduler import open_store

    store = open_store()
    try:
        assert Path(store.db_path) == Path(shared.SCHEDULER_DB_FILE)
    finally:
        store.close()


# ── The guard ────────────────────────────────────────────────────────────────


def test_only_the_scheduler_package_constructs_the_store() -> None:
    """A seventh construction site is how "one place" stops being true.

    Read from the source rather than probed at runtime: a construction in a
    branch the tests do not exercise is exactly the case this guards, and there
    is no runtime probe that sees a dead path.
    """
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "agent").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        if rel.startswith(_STORE_PACKAGE):
            continue
        if _constructions(path.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert not offenders, (
        "SchedulerStore constructed outside agent/scheduler:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse agent.scheduler.open_store(...) instead."
    )


def test_the_store_package_does_construct_it() -> None:
    """Control: the guard is vacuous if construction is not detected at all."""
    total = sum(
        _constructions(path.read_text(encoding="utf-8"))
        for path in (REPO_ROOT / _STORE_PACKAGE).rglob("*.py")
        if "__pycache__" not in path.parts
    )
    assert total >= 1, "the detector found no construction anywhere"


def test_no_module_resolves_the_scheduler_path_directly() -> None:
    """``shared.SCHEDULER_DB_FILE`` is a rewritten global; read it in one place.

    Callers must not resolve it themselves -- that is the six-reader pattern
    this replaced.  The store's own default is the one legitimate reader.
    """
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "agent").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(REPO_ROOT))
        if rel.startswith(_STORE_PACKAGE):
            continue
        source = path.read_text(encoding="utf-8")
        for index, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "shared.SCHEDULER_DB_FILE" not in line:
                continue
            # One exemption, and it is a category rather than a courtesy: a
            # tool that *reports* which database it used is telling the user
            # where their data went, which is not the six-reader problem.  The
            # pattern is narrow on purpose -- a display dict value -- so this
            # cannot quietly absorb a construction.
            if re.search(r'"db_path"\s*:\s*str\(\s*shared\.SCHEDULER_DB_FILE', line):
                continue
            offenders.append(f"{rel}:{index}")
    assert not offenders, (
        "the scheduler database path is resolved outside agent/scheduler:\n  "
        + "\n  ".join(offenders)
    )

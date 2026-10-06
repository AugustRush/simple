"""The Ralph task store is constructed in one place.

The same debt as the scheduler store, one layer down and much smaller:
``RalphTaskStore`` was constructed in three modules, and two of them were
convenience wrappers in ``agent/__init__.py`` that each built a throwaway
instance to save or load one task, reading a module-level ``TASKS_DIR`` that
``_set_agent_home`` rewrites.

Three sites is not twenty-two, so this is a tidy-up rather than a defect.  It is
recorded the same way because the *shape* is what matters for Task 10: a plugin
cannot replace the store it does not construct, and a caller cannot point
somewhere else while the path is resolved from a global.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The only package allowed to name the concrete class in a call.
_STORE_PACKAGE = "agent/ralph"


def _constructions(source: str) -> int:
    tree = ast.parse(source)
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name == "RalphTaskStore":
            count += 1
    return count


# ── The seam ─────────────────────────────────────────────────────────────────


def test_open_task_store_is_the_named_constructor() -> None:
    from agent.ralph import open_task_store

    assert callable(open_task_store)
    assert open_task_store.__module__.startswith("agent.ralph")


def test_open_task_store_takes_an_explicit_directory(tmp_path: Path) -> None:
    from agent.ralph import open_task_store

    store = open_task_store(tmp_path / "tasks")
    assert Path(store.tasks_dir) == (tmp_path / "tasks").resolve()


def test_open_task_store_defaults_to_the_configured_directory() -> None:
    """``None`` resolves through the store's own default, not through a caller."""
    from agent import shared
    from agent.ralph import open_task_store

    store = open_task_store()
    assert Path(store.tasks_dir) == Path(shared.SCHEDULER_DIR).resolve()


# ── The wrappers keep their contract ─────────────────────────────────────────


def test_the_legacy_wrappers_still_round_trip_a_task(tmp_path, monkeypatch) -> None:
    """They read ``agent.TASKS_DIR`` live, which is why the seam must accept a path.

    The existing test for these wrappers monkeypatches that module attribute, so
    a factory that ignored its argument and read a global would break the whole
    point of the exercise.
    """
    import agent
    from agent.ralph import RalphTask

    monkeypatch.setattr(agent, "TASKS_DIR", tmp_path / "tasks")
    task = RalphTask(id="seam123", goal="still works")

    agent._save_ralph_task(task)
    assert agent._load_ralph_task("seam123") == task
    assert agent._load_ralph_task("absent") is None


def test_the_wrappers_follow_a_repointed_directory(tmp_path, monkeypatch) -> None:
    import agent
    from agent.ralph import RalphTask

    first = tmp_path / "one"
    second = tmp_path / "two"
    monkeypatch.setattr(agent, "TASKS_DIR", first)
    agent._save_ralph_task(RalphTask(id="t1", goal="in the first place"))

    monkeypatch.setattr(agent, "TASKS_DIR", second)
    assert agent._load_ralph_task("t1") is None, "the second place must be empty"
    agent._save_ralph_task(RalphTask(id="t1", goal="in the second place"))
    assert agent._load_ralph_task("t1").goal == "in the second place"


# ── The guard ────────────────────────────────────────────────────────────────


def test_only_the_ralph_package_constructs_the_store() -> None:
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
        "RalphTaskStore constructed outside agent/ralph:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse agent.ralph.open_task_store(...) instead."
    )


def test_the_ralph_package_does_construct_it() -> None:
    """Control: the guard is vacuous if construction is not detected at all."""
    total = sum(
        _constructions(path.read_text(encoding="utf-8"))
        for path in (REPO_ROOT / _STORE_PACKAGE).rglob("*.py")
        if "__pycache__" not in path.parts
    )
    assert total >= 1, "the detector found no construction anywhere"

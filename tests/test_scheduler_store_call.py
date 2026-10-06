"""How the scheduler gets a store it can call from a worker thread.

The decision used to be ``type(self.store) is not SchedulerStore`` inside
``SchedulerService._store_call``.  It is now the store's own answer, because only
the SQLite store knows its connection is thread-bound -- and a scheduler that has
to recognise the concrete class is a scheduler a plugin cannot replace.

The old behaviour is preserved exactly, including one thing that looks like a bug
and is kept anyway: the test is by *identity*, so a ``SchedulerStore`` subclass
keeps inheriting its parent's connection.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from agent.scheduler.store import SchedulerStore
from agent.scheduler.store_call import call_store_method


class _PlainStore:
    """A duck-typed double with no opinion about threads."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.thread_ids: list[int] = []

    def record(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.thread_ids.append(threading.get_ident())
        return "direct"


class _ThreadOwner:
    """A store that wants a copy per calling thread, like the SQLite one."""

    def __init__(self, tag: str = "original") -> None:
        self.tag = tag
        self.opened: list[str] = []
        self.thread_ids: list[int] = []

    def _calling_in_thread(self) -> "_ThreadOwner":
        self.opened.append(self.tag)
        return _ThreadOwner(tag=f"{self.tag}-thread")

    def record(self, *args, **kwargs):
        self.thread_ids.append(threading.get_ident())
        return self.tag


# ── The store's own answer ───────────────────────────────────────────────────


def test_a_sqlite_store_hands_out_a_thread_owned_connection(tmp_path: Path) -> None:
    """A fresh connection on the same database, closed by the caller."""
    store = SchedulerStore(db_path=tmp_path / "s.db")
    try:
        thread_store = store._calling_in_thread()
        try:
            assert thread_store is not store
            assert thread_store.db_path == store.db_path
            # It is usable, which is the whole point of handing one out.
            assert thread_store.list_tasks() == []
        finally:
            thread_store.close()
    finally:
        store.close()


def test_a_subclass_keeps_the_parent_connection(tmp_path: Path) -> None:
    """Identity, not isinstance -- the old class test's behaviour, preserved.

    A ``SchedulerStore`` subclass returns itself, so it keeps the connection it
    was built with and has the same thread-affinity problem the check existed to
    avoid.  Kept deliberately: this change moves *where* the decision lives, not
    *what* it decides.  Narrowing it here would be a behavioural change wearing a
    refactor's clothes.
    """

    class _Subclass(SchedulerStore):
        pass

    subclass = _Subclass(db_path=tmp_path / "sub.db")
    try:
        assert subclass._calling_in_thread() is subclass
    finally:
        subclass.close()


def test_the_sqlite_store_does_hand_out_copies(tmp_path: Path) -> None:
    """Control for the test above: the base class is *not* identity-preserving."""
    store = SchedulerStore(db_path=tmp_path / "base.db")
    try:
        assert store._calling_in_thread() is not store
    finally:
        store.close()


# ── Calling through the seam ─────────────────────────────────────────────────


def test_a_store_without_the_hook_is_called_directly() -> None:
    store = _PlainStore()
    result = asyncio.run(call_store_method(store, "record", 1, key="v"))
    assert result == "direct"
    assert store.calls == [((1,), {"key": "v"})]


def test_a_store_with_the_hook_is_called_on_its_thread_copy() -> None:
    """The scheduler must not be the one deciding; the store's copy is used."""
    store = _ThreadOwner()
    result = asyncio.run(call_store_method(store, "record"))
    assert store.opened == ["original"], "the hook was asked"
    assert result == "original-thread", "the copy did the work"


def test_the_call_runs_off_the_calling_thread() -> None:
    """Both branches go through ``asyncio.to_thread``, as before."""
    store = _PlainStore()
    main_thread = threading.get_ident()
    asyncio.run(call_store_method(store, "record"))
    assert store.thread_ids and store.thread_ids[0] != main_thread


def test_the_original_is_never_called_when_a_copy_is_offered() -> None:
    """The copy does the work, so the scheduler's own connection is untouched."""
    store = _ThreadOwner()
    asyncio.run(call_store_method(store, "record"))
    assert store.thread_ids == [], "the original was called on another thread"


def test_arguments_and_keywords_reach_the_method() -> None:
    store = _PlainStore()
    asyncio.run(call_store_method(store, "record", "a", "b", flag=True))
    assert store.calls == [(("a", "b"), {"flag": True})]


def test_a_method_that_raises_propagates() -> None:
    class _Boom:
        def explode(self):
            raise ValueError("store said no")

    with pytest.raises(ValueError, match="store said no"):
        asyncio.run(call_store_method(_Boom(), "explode"))


def test_the_hook_may_be_absent_entirely() -> None:
    """Backwards compatible with any store written before the seam existed."""
    store = _PlainStore()
    assert not hasattr(store, "_calling_in_thread")
    assert asyncio.run(call_store_method(store, "record")) == "direct"


# ── The scheduler no longer knows the class ──────────────────────────────────


def test_the_scheduler_contains_no_concrete_store_test() -> None:
    """The point of the change, asserted at the source.

    A runtime probe would not catch a class test on a branch the tests do not
    exercise, and a class test is exactly what this removed.
    """
    import ast

    source = Path(__file__).resolve().parents[1] / "agent/scheduler/runtime.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        # type(x) is Y  /  isinstance(x, Y)
        if isinstance(node, ast.Compare):
            text = ast.unparse(node)
            if "type(" in text and "SchedulerStore" in text:
                offenders.append(text)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "isinstance"
            and "SchedulerStore" in ast.unparse(node)
        ):
            offenders.append(ast.unparse(node))
    assert not offenders, (
        "runtime.py still judges the store by concrete class:\n  "
        + "\n  ".join(offenders)
    )


def test_the_scheduler_still_constructs_no_store() -> None:
    """It asks the store; it does not build one.  The construction seam holds."""
    source = Path(__file__).resolve().parents[1] / "agent/scheduler/runtime.py"
    assert "SchedulerStore(" not in source.read_text(encoding="utf-8")

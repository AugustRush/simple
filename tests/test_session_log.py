"""One path writes durable runtime history.

It was written from three layers -- ``AgentCore`` for ``turn_finished``, a channel for the
runtime events it drained, and ``ContextManager`` holding both halves.  The cost was not
line count: it was that a caller had to know **which context manager belonged to which
session**, and the chain it used to find out had a step that is wrong by construction (the
factory's base manager has a random staging id, so events recorded through it are filed
under an unrelated session -- the channel carried a comment saying so).

These tests pin the two properties that make the consolidation real: there is **one**
implementation of the append-then-project sequence, and the session an event is filed under
is the session in force rather than the one captured at construction.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class _Store:
    """Records what was appended, and answers the one read the projection makes."""

    def __init__(self) -> None:
        self.appended: list = []
        self.working_state = None

    def append_agent_event(self, **kwargs):
        class _Event:
            pass

        event = _Event()
        event.event_type = kwargs["event_type"]
        event.payload = kwargs["payload"]
        event.created_at = kwargs["created_at"]
        event.session_id = kwargs["session_id"]
        self.appended.append(event)
        return event

    def load_session_working_state(self, session_id):
        return None


def test_the_log_appends_then_projects_from_what_was_stored() -> None:
    """Projection from the stored event, not from the argument.

    A projection derived from the caller's dict could disagree with the log it summarises;
    deriving it from the row that was written cannot.
    """
    from agent.memory.session_log import SessionLog

    store = _Store()
    projected: list = []
    log = SessionLog(
        store,
        lambda: "s1",
        project=lambda **kw: projected.append(kw),
    )

    event = log.append("turn_finished", {"user_content": "hi"}, turn_id="t1")

    assert store.appended == [event]
    assert projected and projected[0]["event_type"] == "turn_finished"
    assert projected[0]["created_at"] == event.created_at


def test_the_log_files_under_the_session_in_force() -> None:
    """Read through a callable, because the staging id is assigned when a session binds.

    Capturing the id at construction is what produced events attributed to an unrelated
    session, which is the bug the channel worked around with a comment.
    """
    from agent.memory.session_log import SessionLog

    store = _Store()
    current = {"session": "before"}
    log = SessionLog(store, lambda: current["session"])

    log.append("a", {})
    current["session"] = "after"
    log.append("b", {})

    assert [e.session_id for e in store.appended] == ["before", "after"]


def test_append_without_a_projection_still_writes() -> None:
    """Control: the projection is optional, the append is not."""
    from agent.memory.session_log import SessionLog

    store = _Store()
    log = SessionLog(store, lambda: "s1")

    log.append("a", {})

    assert len(store.appended) == 1


def test_the_context_manager_delegates_rather_than_reimplementing() -> None:
    """There must be one implementation, which is the whole point of the extraction.

    Asserted on the source because the failure mode is a second copy appearing beside the
    first: `record_runtime_event` doing the work itself again would pass every behavioural
    test above while re-creating exactly the duplication the extraction removed.
    """
    source = (REPO_ROOT / "agent/memory/context.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ContextManager"
    )
    method = next(
        node
        for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "record_runtime_event"
    )
    calls = [
        node.func.attr
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "append" in calls, "it must delegate to the log"
    assert "append_agent_event" not in calls, (
        "writing the store directly here is the second implementation this removed"
    )


def test_the_channel_has_no_fallback_to_another_session_s_log() -> None:
    """The lookup chain's last step was wrong by construction, and it was documented.

    Falling back to the factory's base manager files events under an unrelated session and
    makes tool traces disappear after a browser refresh.  A chain with a known-wrong answer
    at the end is one nobody can check, so the chain is gone rather than shortened.
    """
    source = (REPO_ROOT / "agent/channels/base.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "_record_runtime_event":
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
                if inner.func.id == "resolve":
                    offenders.append(f"L{inner.lineno}")
    assert not offenders, (
        "the session log must come from the session's own state, not from a resolve chain: "
        f"{offenders}"
    )

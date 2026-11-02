"""The join the funnel depends on: a task knows which conversation asked for it.

The bug this pins
-----------------
The patrol reported "55 sessions examined, 0 proposals" for a day. The cause was
not "nothing to do": rule 1 needs to know which *session* owns an unacknowledged
failure, and the join was done on ``request_quote`` -- which holds the user's
words ("每天早上九点提醒我写日报"), not a session id. It never matched anything,
and a funnel that matches nothing reads exactly like a funnel with nothing to find.

A column, not a lookup: "which conversation does this task belong to" is a fact
about creation. Once the words are stored, the session is gone.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from agent.scheduler import (
    DeliveryTarget,
    NewScheduledTask,
    SchedulerStore,
    TriggerSpec,
)

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def tmp_path():
    path = Path("/tmp") / f"simple-autonomy-join-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _task(store, *, session_id: str = "", quote: str = ""):
    return store.create_task(
        NewScheduledTask(
            name="日报",
            kind="agent_prompt",
            trigger=TriggerSpec.once(NOW.isoformat(), "UTC"),
            payload={"prompt": "写日报"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
            request_quote=quote,
            session_id=session_id,
        )
    )


def test_a_task_records_the_session_that_created_it(tmp_path) -> None:
    store = SchedulerStore(db_path=tmp_path / "s.db")
    try:
        task = _task(store, session_id="sess-1")
        assert task.session_id == "sess-1"
        # ...and the words are still the words: the two fields are different facts.
        assert task.request_quote == ""
    finally:
        store.close()


def test_the_quote_is_not_mistaken_for_a_session(tmp_path) -> None:
    """The exact confusion that made the funnel blind."""
    store = SchedulerStore(db_path=tmp_path / "s.db")
    try:
        task = _task(store, session_id="sess-1", quote="每天早上九点提醒我写日报")
        assert task.session_id == "sess-1"
        assert task.request_quote == "每天早上九点提醒我写日报"
        assert task.session_id != task.request_quote
    finally:
        store.close()


def test_a_task_from_before_this_column_reads_as_unowned(tmp_path) -> None:
    """A row written before migration 15 has no session -- and says so, rather than
    guessing one from its quote."""
    store = SchedulerStore(db_path=tmp_path / "s.db")
    try:
        task = _task(store, quote="帮我建一个日报")
        assert task.session_id == ""
    finally:
        store.close()


def test_the_patrol_join_maps_tasks_onto_their_session(tmp_path, monkeypatch) -> None:
    """The join itself: a task with a session id appears under that session."""
    store = SchedulerStore(db_path=tmp_path / "s.db")
    try:
        task = _task(store, session_id="sess-1")
        claimed = store.claim_task_now(task.id, now=NOW)
        assert claimed is not None
        store.complete_run(
            task.id, claimed.run.id, finished_at=NOW, status="failed", error="429"
        )
        snapshot = store.attention_snapshot()
        counts = {str(k): int(v) for k, v in (snapshot.get("counts") or {}).items()}
        assert counts == {task.id: 1}, "the failed run is unacknowledged"

        # What `_autonomy_session_rows` does: the task's own session id, not the
        # quote.
        session_task_ids: dict[str, set[str]] = {}
        for row in store.list_tasks():
            owner = str(getattr(row, "session_id", "") or "").strip()
            if owner:
                session_task_ids.setdefault(owner, set()).add(row.id)

        assert session_task_ids == {"sess-1": {task.id}}
        from agent.autonomy.rules import rule_candidates

        proposals = rule_candidates(
            {
                "session_id": "sess-1",
                "title": "构建排查",
                "last_activity": (NOW - __import__("datetime").timedelta(hours=2)).isoformat(),
                "tasks": [
                    {"task_id": tid, "attention": count}
                    for tid, count in counts.items()
                    if tid in session_task_ids.get("sess-1", ())
                ],
            },
            now=NOW,
        )
        assert [p.context["rule"] for p in proposals] == ["failed-run-unreviewed"]
    finally:
        store.close()

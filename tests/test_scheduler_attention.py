"""The "somebody has to notice" half of scheduled tasks.

A run that fails while nobody is watching is the one failure mode the run
list cannot cover: reading the history only tells you about it if you happen
to open that page, and a scheduled task's whole purpose is to run when you
are not looking.  These tests pin the marker that closes that gap:

1. which terminal statuses count as "needs attention" -- and, just as
   importantly, which do *not*, because a user-requested cancellation is not
   a surprise and must not accumulate into a badge;
2. that seeing a failure clears it exactly once, so the badge cannot be
   silenced by a no-op click and cannot re-appear on the next poll;
3. that upgrading an existing database does not present its history as a
   backlog of unread failures.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from agent.scheduler import (
    ATTENTION_STATUSES,
    DeliveryTarget,
    NewScheduledTask,
    SchedulerStore,
    TriggerSpec,
)

WHEN = datetime(2026, 4, 19, 1, 0, tzinfo=timezone.utc)


@pytest.fixture
def tmp_path():
    """A scratch directory rooted at ``/tmp``.

    Overrides pytest's own fixture, which fails in this environment: its
    per-run root under the system temp directory cannot be created when the
    session is sandboxed, so every test that asks for ``tmp_path`` errors out
    before it runs.  ``/tmp`` is writable and just as disposable.
    """
    path = Path("/tmp") / f"simple-attention-test-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        import shutil

        shutil.rmtree(path, ignore_errors=True)


def _store(tmp_path: Path) -> SchedulerStore:
    return SchedulerStore(db_path=tmp_path / "scheduler.db")


def _new_task(store: SchedulerStore, name: str = "nightly") -> object:
    return store.create_task(
        NewScheduledTask(
            name=name,
            kind="agent_prompt",
            trigger=TriggerSpec.once("2026-04-19T00:00:00+00:00", "UTC"),
            payload={"prompt": "nightly"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
            workspace_root=str(Path("/tmp")),
            permission_profile="read_only",
        )
    )


def _run_to_terminal(
    store: SchedulerStore, task: object, status: str, when: datetime = WHEN
) -> str:
    """Drive one fresh run of ``task`` to a terminal status, return its id.

    Started through the manual path so that each call produces exactly one new
    run for exactly this task: a ``once`` trigger only ever queues a single
    occurrence, and claiming *all* due tasks would strand the run of whichever
    task the caller was not asking about.
    """
    claimed = store.claim_task_now(task.id, now=when)
    assert claimed is not None
    assert store.complete_run(
        task.id,
        claimed.run.id,
        finished_at=when,
        status=status,
        error="boom" if status == "failed" else "",
    )
    return claimed.run.id


# --- what counts ------------------------------------------------------------


def test_failed_run_nobody_looked_at_is_counted(tmp_path):
    """Also covers the scheduled path, so the counter is not trigger-specific."""
    store = _store(tmp_path)
    task = _new_task(store)

    claimed = store.claim_due_tasks(WHEN, lease_seconds=30)[0]
    assert store.complete_run(
        task.id, claimed.run.id, finished_at=WHEN, status="failed", error="boom"
    )

    assert store.unacknowledged_failure_counts() == {task.id: 1}


def test_successful_run_never_asks_for_attention(tmp_path):
    store = _store(tmp_path)
    task = _new_task(store)

    _run_to_terminal(store, task, "succeeded")

    assert store.unacknowledged_failure_counts() == {}


def test_user_requested_cancellation_is_not_an_attention_status(tmp_path):
    """Cancelling is something the user chose, so it must not nag them."""
    store = _store(tmp_path)
    task = _new_task(store)

    _run_to_terminal(store, task, "cancelled")

    assert "cancelled" not in ATTENTION_STATUSES
    assert store.unacknowledged_failure_counts() == {}


def test_interrupted_run_is_counted_because_its_outcome_is_unknown(tmp_path):
    """A lost lease means the agent died mid-run; nobody knows what it did."""
    store = _store(tmp_path)
    task = _new_task(store)
    claimed = store.claim_due_tasks(WHEN, lease_seconds=3)[0]

    recovered = store.recover_stale_runs(WHEN + timedelta(seconds=10))

    assert recovered == 1
    assert store.get_run(task.id, claimed.run.id).status == "interrupted"
    assert store.unacknowledged_failure_counts() == {task.id: 1}


def test_counts_accumulate_per_task_and_ignore_seen_ones(tmp_path):
    store = _store(tmp_path)
    first = _new_task(store, name="first")
    second = _new_task(store, name="second")

    _run_to_terminal(store, first, "failed")
    seen = _run_to_terminal(store, first, "failed")
    _run_to_terminal(store, second, "failed")
    assert store.acknowledge_run(first.id, seen) is True

    assert store.unacknowledged_failure_counts() == {first.id: 1, second.id: 1}


# --- acknowledging ----------------------------------------------------------


def test_acknowledging_a_failure_clears_its_task(tmp_path):
    store = _store(tmp_path)
    task = _new_task(store)
    run_id = _run_to_terminal(store, task, "failed")

    assert store.acknowledge_run(task.id, run_id) is True

    assert store.unacknowledged_failure_counts() == {}
    assert store.get_run(task.id, run_id).acknowledged_at is not None


def test_acknowledging_twice_reports_that_there_was_nothing_left_to_clear(tmp_path):
    """The second click must be honest, so the UI can stop offering it."""
    store = _store(tmp_path)
    task = _new_task(store)
    run_id = _run_to_terminal(store, task, "failed")

    assert store.acknowledge_run(task.id, run_id) is True
    assert store.acknowledge_run(task.id, run_id) is False


def test_a_successful_run_cannot_be_acknowledged(tmp_path):
    """Otherwise the badge could be silenced without anything ever failing."""
    store = _store(tmp_path)
    task = _new_task(store)

    run_id = _run_to_terminal(store, task, "succeeded")

    assert store.acknowledge_run(task.id, run_id) is False


def test_acknowledgement_survives_reopening_the_database(tmp_path):
    store = _store(tmp_path)
    task = _new_task(store)
    run_id = _run_to_terminal(store, task, "failed")
    store.acknowledge_run(task.id, run_id)
    store.close()

    reopened = _store(tmp_path)

    assert reopened.unacknowledged_failure_counts() == {}
    assert reopened.get_run(task.id, run_id).acknowledged_at is not None


def test_a_later_failure_still_asks_for_attention_after_an_earlier_one_was_seen(tmp_path):
    """Clearing one failure must not mute the next one."""
    store = _store(tmp_path)
    task = _new_task(store)

    first = _run_to_terminal(store, task, "failed")
    store.acknowledge_run(task.id, first)
    second = _run_to_terminal(store, task, "failed")

    assert store.unacknowledged_failure_counts() == {task.id: 1}
    assert store.get_run(task.id, second).acknowledged_at is None


# --- bulk clearing ----------------------------------------------------------


def test_clearing_one_task_leaves_other_tasks_untouched(tmp_path):
    store = _store(tmp_path)
    first = _new_task(store, name="first")
    second = _new_task(store, name="second")
    _run_to_terminal(store, first, "failed")
    _run_to_terminal(store, second, "failed")

    cleared = store.acknowledge_failures(first.id)

    assert cleared == 1
    assert store.unacknowledged_failure_counts() == {second.id: 1}


def test_clearing_everything_reports_how_many_were_cleared(tmp_path):
    store = _store(tmp_path)
    first = _new_task(store, name="first")
    second = _new_task(store, name="second")
    _run_to_terminal(store, first, "failed")
    _run_to_terminal(store, second, "failed")

    cleared = store.acknowledge_failures()

    assert cleared == 2
    assert store.unacknowledged_failure_counts() == {}
    # Idempotent: a second sweep has nothing to do and says so.
    assert store.acknowledge_failures() == 0


def test_bulk_clear_ignores_runs_that_never_needed_attention(tmp_path):
    store = _store(tmp_path)
    task = _new_task(store)
    _run_to_terminal(store, task, "succeeded")
    _run_to_terminal(store, task, "failed")

    assert store.acknowledge_failures() == 1
    assert store.unacknowledged_failure_counts() == {}


# --- upgrading an existing database ----------------------------------------


def test_upgrade_treats_pre_existing_history_as_already_seen(tmp_path):
    """A user upgrading must not be told their whole history is unread.

    Those runs were reported the old way -- through the run list -- so
    announcing them as new failures the first time the badge is ever shown
    would be a false alarm about work the user already knew about.
    """
    db_path = tmp_path / "scheduler.db"
    store = _store(tmp_path)
    task = _new_task(store)
    old_run = _run_to_terminal(store, task, "failed")
    store.close()

    # Rewind the database to what it looked like before this column existed.
    legacy = sqlite3.connect(db_path)
    with legacy:
        legacy.execute("ALTER TABLE scheduled_task_runs DROP COLUMN acknowledged_at")
        legacy.execute("PRAGMA user_version = 5")
    legacy.close()

    upgraded = _store(tmp_path)

    assert upgraded.unacknowledged_failure_counts() == {}
    assert upgraded.get_run(task.id, old_run).acknowledged_at is not None


def test_upgrade_still_counts_failures_that_happen_afterwards(tmp_path):
    db_path = tmp_path / "scheduler.db"
    store = _store(tmp_path)
    task = _new_task(store)
    store.close()

    legacy = sqlite3.connect(db_path)
    with legacy:
        legacy.execute("ALTER TABLE scheduled_task_runs DROP COLUMN acknowledged_at")
        legacy.execute("PRAGMA user_version = 5")
    legacy.close()

    upgraded = _store(tmp_path)
    _run_to_terminal(upgraded, task, "failed")

    assert upgraded.unacknowledged_failure_counts() == {task.id: 1}

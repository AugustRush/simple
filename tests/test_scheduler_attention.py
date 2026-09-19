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

from agent.channels.web import _attention_payload
from agent.scheduler import (
    ATTENTION_STATUSES,
    DeliveryTarget,
    NewScheduledTask,
    SchedulerStore,
    TriggerSpec,
    Workflow,
    WorkflowStep,
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

    assert store.unacknowledged_attention_counts() == {task.id: 1}


def test_successful_run_never_asks_for_attention(tmp_path):
    store = _store(tmp_path)
    task = _new_task(store)

    _run_to_terminal(store, task, "succeeded")

    assert store.unacknowledged_attention_counts() == {}


def test_user_requested_cancellation_is_not_an_attention_status(tmp_path):
    """Cancelling is something the user chose, so it must not nag them."""
    store = _store(tmp_path)
    task = _new_task(store)

    _run_to_terminal(store, task, "cancelled")

    assert "cancelled" not in ATTENTION_STATUSES
    assert store.unacknowledged_attention_counts() == {}


def test_interrupted_run_is_counted_because_its_outcome_is_unknown(tmp_path):
    """A lost lease means the agent died mid-run; nobody knows what it did."""
    store = _store(tmp_path)
    task = _new_task(store)
    claimed = store.claim_due_tasks(WHEN, lease_seconds=3)[0]

    recovered = store.recover_stale_runs(WHEN + timedelta(seconds=10))

    assert recovered == 1
    assert store.get_run(task.id, claimed.run.id).status == "interrupted"
    assert store.unacknowledged_attention_counts() == {task.id: 1}


def test_counts_accumulate_per_task_and_ignore_seen_ones(tmp_path):
    store = _store(tmp_path)
    first = _new_task(store, name="first")
    second = _new_task(store, name="second")

    _run_to_terminal(store, first, "failed")
    seen = _run_to_terminal(store, first, "failed")
    _run_to_terminal(store, second, "failed")
    assert store.acknowledge_run(first.id, seen) is True

    assert store.unacknowledged_attention_counts() == {first.id: 1, second.id: 1}


# --- acknowledging ----------------------------------------------------------


def test_acknowledging_a_failure_clears_its_task(tmp_path):
    store = _store(tmp_path)
    task = _new_task(store)
    run_id = _run_to_terminal(store, task, "failed")

    assert store.acknowledge_run(task.id, run_id) is True

    assert store.unacknowledged_attention_counts() == {}
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

    assert reopened.unacknowledged_attention_counts() == {}
    assert reopened.get_run(task.id, run_id).acknowledged_at is not None


def test_a_later_failure_still_asks_for_attention_after_an_earlier_one_was_seen(tmp_path):
    """Clearing one failure must not mute the next one."""
    store = _store(tmp_path)
    task = _new_task(store)

    first = _run_to_terminal(store, task, "failed")
    store.acknowledge_run(task.id, first)
    second = _run_to_terminal(store, task, "failed")

    assert store.unacknowledged_attention_counts() == {task.id: 1}
    assert store.get_run(task.id, second).acknowledged_at is None


# --- bulk clearing ----------------------------------------------------------


def test_clearing_one_task_leaves_other_tasks_untouched(tmp_path):
    store = _store(tmp_path)
    first = _new_task(store, name="first")
    second = _new_task(store, name="second")
    _run_to_terminal(store, first, "failed")
    _run_to_terminal(store, second, "failed")

    cleared = store.acknowledge_attention(first.id)

    assert cleared == 1
    assert store.unacknowledged_attention_counts() == {second.id: 1}


def test_clearing_everything_reports_how_many_were_cleared(tmp_path):
    store = _store(tmp_path)
    first = _new_task(store, name="first")
    second = _new_task(store, name="second")
    _run_to_terminal(store, first, "failed")
    _run_to_terminal(store, second, "failed")

    cleared = store.acknowledge_attention()

    assert cleared == 2
    assert store.unacknowledged_attention_counts() == {}
    # Idempotent: a second sweep has nothing to do and says so.
    assert store.acknowledge_attention() == 0


def test_bulk_clear_ignores_runs_that_never_needed_attention(tmp_path):
    store = _store(tmp_path)
    task = _new_task(store)
    _run_to_terminal(store, task, "succeeded")
    _run_to_terminal(store, task, "failed")

    assert store.acknowledge_attention() == 1
    assert store.unacknowledged_attention_counts() == {}


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

    assert upgraded.unacknowledged_attention_counts() == {}
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

    assert upgraded.unacknowledged_attention_counts() == {task.id: 1}


# --- the list behind the count ---------------------------------------------
#
# The badge on the navigation used to be a number with nothing behind it: the
# count walked every task, the workflows tab counted only the steps of
# workflows that still existed, and the task list showed no count at all. A
# badge saying two therefore pointed at a page where two could not be found.
#
# The fix is one question answered once -- `unacknowledged_attention_runs` is
# the list, `unacknowledged_attention_counts` is the count, and the endpoint
# sends them together. What follows pins that they stay the same fact.


def test_the_list_agrees_with_the_count_task_by_task(tmp_path):
    """Two queries over one clause is the arrangement that drifts.

    This is the test that catches it: the badge's number and the rows behind it
    have to be the same thing, or the number cannot be checked.
    """
    store = _store(tmp_path)
    behind = _new_task(store, name="behind")
    punctual = _new_task(store, name="punctual")

    _run_to_terminal(store, behind, "failed")
    _run_to_terminal(store, behind, "interrupted")
    _run_to_terminal(store, punctual, "failed")

    runs = store.unacknowledged_attention_runs()
    by_task: dict[str, int] = {}
    for run in runs:
        by_task[run.task_id] = by_task.get(run.task_id, 0) + 1

    assert store.unacknowledged_attention_counts() == {behind.id: 2, punctual.id: 1}
    assert by_task == {behind.id: 2, punctual.id: 1}
    assert len(runs) == 3


def test_a_standalone_task_appears_in_the_list(tmp_path):
    """The case that was invisible: a task belonging to no workflow.

    Both of the runs waiting in a real database were standalone, and the
    interface had nowhere to show them -- the workflows tab did not count them
    and the task list did not mark them.
    """
    store = _store(tmp_path)
    task = _new_task(store)
    assert task.workflow_id == ""

    run_id = _run_to_terminal(store, task, "failed")

    runs = store.unacknowledged_attention_runs()

    assert [run.id for run in runs] == [run_id]
    assert runs[0].task_id == task.id
    assert runs[0].status == "failed"
    assert runs[0].error == "boom"


def test_a_workflow_step_appears_in_the_list_too(tmp_path):
    """Steps are not excluded: the list is the union, not a view of one tab."""
    store = _store(tmp_path)
    workflow = store.create_workflow(
        Workflow(
            name="nightly report",
            steps=[
                WorkflowStep(
                    key="collect",
                    kind="agent_prompt",
                    name="collect",
                    payload={"prompt": "collect"},
                    depends_on=[],
                    trigger=TriggerSpec.once("2026-04-19T00:00:00+00:00", "UTC"),
                ),
                WorkflowStep(
                    key="analyze",
                    kind="agent_prompt",
                    name="analyze",
                    payload={"prompt": "analyze"},
                    depends_on=["collect"],
                ),
            ],
        )
    )
    collect = store.step_tasks(workflow.id)["collect"]

    claimed = store.claim_due_tasks(WHEN, lease_seconds=30)[0]
    assert store.complete_run(
        collect.id, claimed.run.id, finished_at=WHEN, status="failed", error="step blew up"
    )

    runs = store.unacknowledged_attention_runs()

    assert [run.task_id for run in runs] == [collect.id]
    assert store.unacknowledged_attention_counts() == {collect.id: 1}


def test_newest_first_so_the_first_row_is_the_one_to_open(tmp_path):
    """The interface opens the first run it finds for a task.

    Were the order oldest first, "查看运行" would land on the stale one and the
    person would be back to reading a history to find the recent failure.
    """
    store = _store(tmp_path)
    task = _new_task(store)

    older = _run_to_terminal(store, task, "failed", when=WHEN)
    newer = _run_to_terminal(store, task, "failed", when=WHEN + timedelta(hours=1))

    runs = store.unacknowledged_attention_runs()

    assert [run.id for run in runs] == [newer, older]


def test_acknowledging_takes_it_out_of_the_list(tmp_path):
    store = _store(tmp_path)
    task = _new_task(store)
    run_id = _run_to_terminal(store, task, "failed")

    assert len(store.unacknowledged_attention_runs()) == 1
    assert store.acknowledge_run(task.id, run_id) is True
    assert store.unacknowledged_attention_runs() == []
    assert store.unacknowledged_attention_counts() == {}


def test_the_cap_truncates_the_list_without_lowering_the_count(tmp_path):
    """A capped list must not quietly become a smaller number.

    The count is the promise the badge makes; the cap is only about how much
    one response carries.
    """
    store = _store(tmp_path)
    task = _new_task(store)
    for _ in range(3):
        _run_to_terminal(store, task, "failed")

    assert len(store.unacknowledged_attention_runs(limit=2)) == 2
    assert store.unacknowledged_attention_counts() == {task.id: 3}


# The snapshot is one question answered once, under one lock: the total the
# badge shows, the per-task counts the cards show, the rows behind both, and
# the run to open per task all come out of the same query. What follows pins
# that they stay one fact.


def test_the_snapshot_adds_up_to_the_counts_it_carries(tmp_path):
    """The number, the per-task counts, and the rows are one answer."""
    store = _store(tmp_path)
    behind = _new_task(store, name="behind")
    punctual = _new_task(store, name="punctual")

    # Distinct moments, so the ordering assertion is about the snapshot's
    # own newest-first promise and not about tie-breaking on ids.
    _run_to_terminal(store, behind, "failed", when=WHEN)
    _run_to_terminal(store, behind, "interrupted", when=WHEN + timedelta(minutes=1))
    _run_to_terminal(store, punctual, "failed", when=WHEN + timedelta(minutes=2))

    snapshot = store.attention_snapshot()

    assert snapshot["total"] == 3
    assert snapshot["counts"] == {behind.id: 2, punctual.id: 1}
    assert [run.task_id for run in snapshot["runs"]] == [
        punctual.id, behind.id, behind.id,
    ]
    # Every task's count is the size of its own row set, checked from the
    # snapshot itself rather than against a second query.
    counted: dict[str, int] = {}
    for run in snapshot["runs"]:
        counted[run.task_id] = counted.get(run.task_id, 0) + 1
    assert counted == snapshot["counts"]


def test_the_snapshot_points_at_each_tasks_newest_waiting_run(tmp_path):
    """``latest_by_task`` is the run "查看运行" should open.

    Newest first, because the newest failure is the one the number is about;
    and derived in the store rather than from whatever rows survived the cap,
    because a task whose rows have all fallen out of a capped list still has
    a count on its card -- and its click has to land somewhere better than
    "the newest run, which is usually fine".
    """
    store = _store(tmp_path)
    task = _new_task(store)
    older = _run_to_terminal(store, task, "failed", when=WHEN)
    newest = _run_to_terminal(store, task, "failed", when=WHEN + timedelta(hours=1))
    other = _new_task(store, name="other")
    other_run = _run_to_terminal(store, other, "failed", when=WHEN + timedelta(hours=2))

    snapshot = store.attention_snapshot()

    assert snapshot["latest_by_task"] == {task.id: newest, other.id: other_run}
    assert older != newest

    # The cap is where the client-side approximation broke: truncate the list
    # below the number of runs and the map still knows every task's run.
    capped = store.attention_snapshot(limit=2)
    assert len(capped["runs"]) == 2
    assert capped["total"] == 3
    assert capped["latest_by_task"] == {task.id: newest, other.id: other_run}


def test_the_cap_bounds_the_rows_without_hiding_a_task(tmp_path):
    """The regression: a task can sit entirely below the cap.

    The earlier cap test happened not to catch this -- both of its tasks had
    their newest run inside the truncated window, so every task was still
    mentioned by the rows that came back.  Give one task nothing but old runs
    and it falls out of the list completely, which is where deriving the
    counts from those rows went wrong: the card kept showing a number the
    payload no longer explained, ``sum(counts)`` stopped matching ``total``,
    and the click had no run to open.
    """
    store = _store(tmp_path)
    forgotten = _new_task(store, name="forgotten")
    forgotten_run = _run_to_terminal(store, forgotten, "failed", when=WHEN)
    loud = _new_task(store, name="loud")
    for minute in range(1, 4):
        _run_to_terminal(
            store, loud, "failed", when=WHEN + timedelta(minutes=minute)
        )

    # Three newest runs all belong to `loud`, so `forgotten` is not in `runs`.
    snapshot = store.attention_snapshot(limit=3)

    assert [run.task_id for run in snapshot["runs"]] == [loud.id] * 3
    assert snapshot["counts"] == {loud.id: 3, forgotten.id: 1}
    assert snapshot["total"] == 4
    assert sum(snapshot["counts"].values()) == snapshot["total"]
    assert snapshot["latest_by_task"][forgotten.id] == forgotten_run


def test_the_snapshot_counts_agree_with_the_uncapped_query(tmp_path):
    """One rule, two readers: the snapshot and the standalone count method.

    They are the same question, and the endpoints mix them freely -- some read
    ``counts`` from the snapshot, others still call the count method -- so a
    disagreement between them would show up as a card whose number changes
    depending on which endpoint last answered.
    """
    store = _store(tmp_path)
    quiet = _new_task(store, name="quiet")
    busy = _new_task(store, name="busy")
    _run_to_terminal(store, quiet, "failed", when=WHEN)
    for minute in range(1, 5):
        _run_to_terminal(
            store, busy, "interrupted", when=WHEN + timedelta(minutes=minute)
        )

    assert store.attention_snapshot(limit=1)["counts"] == (
        store.unacknowledged_attention_counts()
    )


def test_the_snapshot_is_empty_when_nothing_waits(tmp_path):
    store = _store(tmp_path)
    _new_task(store)

    snapshot = store.attention_snapshot()

    assert snapshot == {"total": 0, "counts": {}, "runs": [], "latest_by_task": {}}


def test_the_payload_number_is_the_length_of_the_list_it_carries(tmp_path):
    """One payload, so the badge and the rows cannot come from different places."""
    store = _store(tmp_path)
    task = _new_task(store)
    run_id = _run_to_terminal(store, task, "failed")

    payload = _attention_payload(store)

    assert payload["unseen_attention"] == 1
    assert len(payload["attention_runs"]) == 1
    assert payload["attention_runs"][0]["run_id"] == run_id
    assert payload["attention_runs"][0]["task_id"] == task.id


def test_the_payload_names_the_task_so_the_row_can_be_recognised(tmp_path):
    """A row that says only "failed" sends the reader back to hunting."""
    store = _store(tmp_path)
    task = _new_task(store, name="A股模拟盘每日结算")
    _run_to_terminal(store, task, "failed")

    item = _attention_payload(store)["attention_runs"][0]

    assert item["task_name"] == "A股模拟盘每日结算"
    assert item["error"] == "boom"
    assert item["workflow_id"] == ""
    assert item["workflow_name"] == ""
    assert item["workflow_deleted"] is False


def test_the_payload_says_when_the_workflow_is_gone(tmp_path):
    """A step of a deleted workflow still has to be locatable.

    Deleting a workflow leaves its steps behind on purpose -- their history is
    the record that it ran -- and from that moment nothing owns them. A row
    showing a blank origin would be a count with a hole in it, since the number
    includes it.
    """
    store = _store(tmp_path)
    workflow = store.create_workflow(
        Workflow(
            name="nightly report",
            steps=[
                WorkflowStep(
                    key="collect",
                    kind="agent_prompt",
                    name="collect",
                    payload={"prompt": "collect"},
                    depends_on=[],
                    trigger=TriggerSpec.once("2026-04-19T00:00:00+00:00", "UTC"),
                ),
            ],
        )
    )
    collect = store.step_tasks(workflow.id)["collect"]
    claimed = store.claim_due_tasks(WHEN, lease_seconds=30)[0]
    store.complete_run(collect.id, claimed.run.id, finished_at=WHEN, status="failed")

    store.delete_workflow(workflow.id)

    item = _attention_payload(store)["attention_runs"][0]

    assert item["run_id"] == claimed.run.id
    assert item["workflow_id"] == workflow.id
    assert item["workflow_name"] == ""
    assert item["workflow_deleted"] is True


def test_an_empty_payload_is_zero_and_no_rows(tmp_path):
    store = _store(tmp_path)
    _new_task(store)

    payload = _attention_payload(store)

    assert payload["unseen_attention"] == 0
    assert payload["attention_runs"] == []

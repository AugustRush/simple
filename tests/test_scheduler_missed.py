"""What happened while nothing was running.

A scheduled task exists to run when nobody is looking, which means the one
period it cannot report on is the period it was not running.  The claim path
moves a task's cursor straight to the next occurrence in the future; every
occurrence it stepped over simply stops existing.  The task then looks healthy:
its runs succeed, its next fire time is right, and the daily report that did
not happen for four days leaves no trace anywhere.

These tests pin the record that closes that gap:

1. the count is derived from each trigger's own calendar, so it cannot drift
   away from the schedule it describes;
2. a run that resumes a schedule carries the count, and the run that follows
   normally carries nothing -- the marker belongs to the resuming run, not to
   every run after it;
3. an offline gap reaches the "somebody has to look at this" marker even
   though the run itself succeeded, because nothing else in the system will
   ever mention it;
4. upgrading an existing database neither invents history nor loses it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from agent.scheduler import (
    DeliveryTarget,
    NewScheduledTask,
    SchedulerStore,
    TriggerSpec,
    run_needs_attention,
)
from agent.scheduler.models import describe_missed_occurrences

UTC = timezone.utc


@pytest.fixture
def tmp_path():
    """A scratch directory rooted at ``/tmp``.

    Overrides pytest's own fixture, which fails in this environment: its
    per-run root under the system temp directory cannot be created when the
    session is sandboxed, so every test that asks for ``tmp_path`` errors out
    before it runs.  ``/tmp`` is writable and just as disposable.
    """
    path = Path("/tmp") / f"simple-missed-test-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    yield path
    for child in sorted(path.rglob("*"), reverse=True):
        child.unlink() if child.is_file() else child.rmdir()
    path.rmdir()


def make_store(tmp_path: Path) -> SchedulerStore:
    return SchedulerStore(db_path=tmp_path / "scheduler.db")


def add_daily_task(store: SchedulerStore, created_at: datetime, **overrides):
    """A 09:00 daily task whose first fire time is pinned.

    ``create_task(now=...)`` is what makes the setup deterministic: the cursor
    is computed from that instant, so "the 09:00 run on the 20th" is a fact of
    the test rather than a function of when it happens to be run.
    """
    task = NewScheduledTask(
        name=overrides.pop("name", "daily-report"),
        kind="agent_prompt",
        trigger=TriggerSpec.daily("09:00", "UTC"),
        payload={"prompt": "生成日报"},
        delivery_mode="standalone",
        delivery_target=DeliveryTarget.standalone(),
        workspace_root="/tmp",
        **overrides,
    )
    return store.create_task(task, now=created_at)


def claim(store: SchedulerStore, now: datetime):
    """One run, as the scheduler loop would claim it at ``now``."""
    claimed = store.claim_due_tasks(now=now, limit=5, lease_seconds=300)
    assert claimed, "expected the task to be due"
    return claimed[0]


def finish(store: SchedulerStore, task_id: str, run_id: str, status="succeeded", **kw):
    assert store.complete_run(
        task_id, run_id, finished_at=datetime.now(UTC), status=status, **kw
    )


MONDAY = datetime(2026, 4, 20, 8, 0, tzinfo=UTC)  # an hour before the 09:00 run
MONDAY_RUN = datetime(2026, 4, 20, 9, 0, tzinfo=UTC)
THURSDAY = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)


# ── The count itself ────────────────────────────────────────────────────────


def test_daily_trigger_counts_the_days_it_jumped_over():
    """Mon 09:00 due, nothing ran until Thu midday -> Tue, Wed, Thu are gone.

    Exclusive of both ends: the Monday run is the one being claimed (it is
    late, not missing) and Friday is the next occurrence, which has not come
    round yet.
    """
    trigger = TriggerSpec.daily("09:00", "UTC")

    next_run_at = trigger.advance_after_claim(MONDAY_RUN, THURSDAY)

    assert next_run_at == datetime(2026, 4, 24, 9, 0, tzinfo=UTC)  # Friday
    assert trigger.count_missed(MONDAY_RUN, next_run_at) == 3


def test_a_schedule_that_did_not_gap_reports_nothing():
    """The ordinary case must stay silent, or the signal is worthless."""
    trigger = TriggerSpec.daily("09:00", "UTC")

    next_run_at = trigger.advance_after_claim(
        MONDAY_RUN, datetime(2026, 4, 20, 9, 0, 30, tzinfo=UTC)
    )

    assert next_run_at == datetime(2026, 4, 21, 9, 0, tzinfo=UTC)
    assert trigger.count_missed(MONDAY_RUN, next_run_at) == 0


def test_interval_trigger_counts_by_its_own_step():
    """Half-hourly, offline for three hours -> six occurrences never fired."""
    trigger = TriggerSpec.interval(30, "minutes", "2026-04-20T00:00:00+00:00", "UTC")
    scheduled_for = datetime(2026, 4, 20, 0, 0, tzinfo=UTC)
    now = datetime(2026, 4, 20, 3, 5, tzinfo=UTC)

    next_run_at = trigger.advance_after_claim(scheduled_for, now)

    assert next_run_at == datetime(2026, 4, 20, 3, 30, tzinfo=UTC)
    assert trigger.count_missed(scheduled_for, next_run_at) == 6


def test_weekdays_trigger_does_not_count_the_weekend_as_missed():
    """The count comes from the trigger's own calendar, not from elapsed time.

    A Friday-to-Monday gap is one skipped occurrence, not three: Saturday and
    Sunday were never scheduled.  Anything that divided elapsed time by an
    interval would get this wrong, which is why the count is walked out of
    ``next_after`` instead.
    """
    trigger = TriggerSpec.weekdays("09:00", "UTC")
    scheduled_for = datetime(2026, 4, 24, 9, 0, tzinfo=UTC)  # Friday
    now = datetime(2026, 4, 27, 10, 0, tzinfo=UTC)  # Monday

    next_run_at = trigger.advance_after_claim(scheduled_for, now)

    assert next_run_at == datetime(2026, 4, 28, 9, 0, tzinfo=UTC)  # Tuesday
    assert trigger.count_missed(scheduled_for, next_run_at) == 1


def test_weekly_trigger_counts_whole_weeks_only():
    trigger = TriggerSpec.weekly("mon", "09:00", "UTC")
    scheduled_for = datetime(2026, 4, 20, 9, 0, tzinfo=UTC)  # Monday
    now = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)  # two Mondays later

    next_run_at = trigger.advance_after_claim(scheduled_for, now)

    # 27 Apr and 4 May were skipped; 11 May is next.
    assert next_run_at == datetime(2026, 5, 11, 9, 0, tzinfo=UTC)
    assert trigger.count_missed(scheduled_for, next_run_at) == 2


def test_a_once_trigger_running_late_is_not_a_miss():
    """A one-shot that fires late still fired, so nothing was skipped.

    Its cursor has nowhere to advance to, and "no next occurrence" must not be
    read as "everything was missed".
    """
    trigger = TriggerSpec.once("2026-04-20T09:00:00+00:00", "UTC")

    next_run_at = trigger.advance_after_claim(
        MONDAY_RUN, datetime(2026, 4, 25, 9, 0, tzinfo=UTC)
    )

    assert next_run_at is None
    assert trigger.count_missed(MONDAY_RUN, next_run_at) == 0


def test_the_walk_is_bounded():
    """A minutely task offline for years must not stall the claim transaction.

    The count exists to be read by a person, and "at least a thousand" is
    already more than anyone acts on differently.
    """
    trigger = TriggerSpec.interval(1, "minutes", "2020-01-01T00:00:00+00:00", "UTC")
    scheduled_for = datetime(2020, 1, 1, tzinfo=UTC)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    next_run_at = trigger.advance_after_claim(scheduled_for, now)

    assert trigger.count_missed(scheduled_for, next_run_at) == (
        TriggerSpec.MISSED_COUNT_LIMIT
    )
    assert "至少" in describe_missed_occurrences(TriggerSpec.MISSED_COUNT_LIMIT)


# ── The record ──────────────────────────────────────────────────────────────


def test_a_resuming_run_carries_the_count_and_the_next_one_does_not(tmp_path):
    """The marker belongs to the run that resumes the schedule, not to all.

    Repeating it on every later run would turn one offline afternoon into a
    permanent label and make the number meaningless.
    """
    store = make_store(tmp_path)
    try:
        task = add_daily_task(store, created_at=MONDAY)

        resuming = claim(store, THURSDAY)
        assert resuming.run.scheduled_for == MONDAY_RUN
        assert resuming.run.missed_count == 3

        finish(store, task.id, resuming.run.id)

        on_time = claim(store, datetime(2026, 4, 24, 9, 0, tzinfo=UTC))
        assert on_time.run.missed_count == 0
    finally:
        store.close()


def test_the_count_survives_a_reopen(tmp_path):
    store = make_store(tmp_path)
    try:
        task = add_daily_task(store, created_at=MONDAY)
        claimed = claim(store, THURSDAY)
        run_id, task_id = claimed.run.id, task.id
    finally:
        store.close()

    reopened = make_store(tmp_path)
    try:
        stored = reopened.get_run(task_id, run_id)
        assert stored is not None
        assert stored.missed_count == 3
    finally:
        reopened.close()


def test_a_run_that_missed_nothing_reads_as_zero(tmp_path):
    store = make_store(tmp_path)
    try:
        task = add_daily_task(store, created_at=MONDAY)
        claimed = claim(store, MONDAY_RUN)

        assert claimed.run.missed_count == 0
        assert describe_missed_occurrences(claimed.run.missed_count) == ""
    finally:
        store.close()


def test_each_task_keeps_its_own_count(tmp_path):
    """One task's offline gap must not be attributed to another's run.

    Both tasks are claimed in the same poll; only the one whose cursor fell
    days behind reports a gap.  A task-level flag, or a count written to the
    wrong row, would smear one task's downtime across everything the loop
    happened to pick up in the same pass.
    """
    store = make_store(tmp_path)
    try:
        behind = add_daily_task(store, MONDAY, name="behind")
        # Created the same morning it is due, so its cursor never fell behind.
        punctual = add_daily_task(
            store, datetime(2026, 4, 23, 8, 0, tzinfo=UTC), name="punctual"
        )

        claimed = {
            item.task.name: item.run
            for item in store.claim_due_tasks(
                now=THURSDAY, limit=5, lease_seconds=300
            )
        }

        assert set(claimed) == {"behind", "punctual"}
        assert claimed["behind"].missed_count == 3
        assert claimed["punctual"].missed_count == 0
        assert behind.id != punctual.id
    finally:
        store.close()


# ── The two halves of "somebody has to look at this" ────────────────────────


def test_a_skipped_schedule_asks_for_attention_even_though_it_succeeded(tmp_path):
    """The run succeeds, so nothing else would ever mention the gap.

    This is the whole reason the count is part of the attention marker instead
    of a line in a run detail that nobody opens: a daily report can be missing
    for a week with every run marked fine.
    """
    store = make_store(tmp_path)
    try:
        task = add_daily_task(store, created_at=MONDAY)
        claimed = claim(store, THURSDAY)
        finish(store, task.id, claimed.run.id, summary="日报已生成")

        stored = store.get_run(task.id, claimed.run.id)
        assert stored.status == "succeeded"
        assert run_needs_attention(stored) is True
        assert store.unacknowledged_attention_counts() == {task.id: 1}
    finally:
        store.close()


def test_the_python_predicate_and_the_sql_clause_agree(tmp_path):
    """The rule is written twice -- once per run, once as a count.

    SQL cannot call the predicate, so the two are kept beside each other and
    checked here; a drift between them would show a badge counting a run that
    the interface refuses to mark.
    """
    store = make_store(tmp_path)
    try:
        task = add_daily_task(store, created_at=MONDAY)

        # A run that skipped occurrences, and succeeded.
        skipped = claim(store, THURSDAY)
        finish(store, task.id, skipped.run.id, summary="ok")

        # A run that missed nothing, and failed.
        failed = claim(store, datetime(2026, 4, 24, 9, 0, tzinfo=UTC))
        finish(store, task.id, failed.run.id, status="failed", error="boom")

        # A run that missed nothing and succeeded: neither way in.
        fine = claim(store, datetime(2026, 4, 25, 9, 0, tzinfo=UTC))
        finish(store, task.id, fine.run.id, summary="ok")

        counted = store.unacknowledged_attention_counts()
        reloaded = [
            store.get_run(task.id, item.run.id) for item in (skipped, failed, fine)
        ]
        expected = sum(1 for run in reloaded if run_needs_attention(run))

        assert counted == {task.id: 2}
        assert expected == 2
        assert reloaded[0].missed_count == 3
        assert reloaded[1].missed_count == 0
        assert reloaded[2].missed_count == 0
    finally:
        store.close()


def test_acknowledging_a_skipped_schedule_clears_it_once(tmp_path):
    store = make_store(tmp_path)
    try:
        task = add_daily_task(store, created_at=MONDAY)
        claimed = claim(store, THURSDAY)
        finish(store, task.id, claimed.run.id, summary="ok")

        assert store.acknowledge_run(task.id, claimed.run.id) is True
        assert store.acknowledge_run(task.id, claimed.run.id) is False
        assert store.unacknowledged_attention_counts() == {}
        assert run_needs_attention(store.get_run(task.id, claimed.run.id)) is False
    finally:
        store.close()


def test_a_cancelled_run_that_skipped_occurrences_still_asks(tmp_path):
    """Cancellation is not a surprise, but the gap before it is.

    ``cancelled`` is deliberately outside ``ATTENTION_STATUSES`` so a
    user-requested stop does not accumulate into a badge; the skipped
    occurrences it carried are a different fact and are still worth saying.
    """
    store = make_store(tmp_path)
    try:
        task = add_daily_task(store, created_at=MONDAY)
        claimed = claim(store, THURSDAY)
        finish(store, task.id, claimed.run.id, status="cancelled", error="cancelled")

        stored = store.get_run(task.id, claimed.run.id)
        assert stored.status == "cancelled"
        assert run_needs_attention(stored) is True
    finally:
        store.close()


# ── Upgrading an existing database ──────────────────────────────────────────


def test_upgrading_an_older_database_adds_the_column_without_inventing_history(
    tmp_path,
):
    """An upgraded database must not claim its old runs skipped anything.

    The count is not derivable after the fact, so the honest value for runs
    that predate the column is zero -- "we did not record this" -- not a
    guess.  It also keeps the release from announcing a backlog of attention
    on the first launch after the upgrade.
    """
    db_path = tmp_path / "scheduler.db"
    old = SchedulerStore(db_path=db_path)
    try:
        task = add_daily_task(old, created_at=MONDAY)
        claimed = claim(old, MONDAY_RUN)
        finish(old, task.id, claimed.run.id, summary="旧记录")
        run_id, task_id = claimed.run.id, task.id
        # Keep the row, drop the column and the version marker: this is what a
        # database written by the previous release looks like.
        old._conn.execute(
            "ALTER TABLE scheduled_task_runs DROP COLUMN missed_count"
        )
        old._conn.execute("PRAGMA user_version = 6")
        old._conn.commit()
    finally:
        old.close()

    upgraded = SchedulerStore(db_path=db_path)
    try:
        assert upgraded.SCHEMA_VERSION == 7
        row = upgraded._conn.execute(
            "SELECT missed_count FROM scheduled_task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row["missed_count"] == 0
        assert upgraded.unacknowledged_attention_counts() == {}
        # The task and its run are intact; only the new fact is absent.
        assert upgraded.get_task(task_id) is not None
        assert upgraded.get_run(task_id, run_id).summary == "旧记录"
    finally:
        upgraded.close()


def test_a_new_gap_after_the_upgrade_still_counts(tmp_path):
    db_path = tmp_path / "scheduler.db"
    old = SchedulerStore(db_path=db_path)
    try:
        task = add_daily_task(old, created_at=MONDAY)
        task_id = task.id
    finally:
        old.close()

    # Reopening runs the migration, which is the path a real upgrade takes.
    upgraded = SchedulerStore(db_path=db_path)
    try:
        claimed = claim(upgraded, THURSDAY)
        assert claimed.run.missed_count == 3
    finally:
        upgraded.close()


def test_the_column_is_declared_not_null_with_a_default(tmp_path):
    """A queued retry inserts a run without naming the column.

    ``enqueue_retry`` has its own INSERT, so the default is what keeps that
    path working rather than raising on a NOT NULL violation.
    """
    store = make_store(tmp_path)
    try:
        info = {
            row[1]: row
            for row in store._conn.execute(
                "PRAGMA table_info(scheduled_task_runs)"
            ).fetchall()
        }
        column = info["missed_count"]
        assert column[3] == 1  # notnull
        assert column[4] == "0"  # dflt_value
    finally:
        store.close()


# ── End to end: the gap reaches the run that resumes the schedule ───────────


def test_the_run_summary_says_what_was_skipped(tmp_path):
    """The count has to survive into the words a person reads.

    A number in a column that nothing renders is a record nobody keeps.  The
    executor returns its own summary, so the note has to be composed with it
    rather than replacing it -- losing the actual result to report the gap
    would trade one silence for another.
    """
    import asyncio

    from agent.scheduler import ExecutionResult, SchedulerService

    store = make_store(tmp_path)
    try:
        task = add_daily_task(store, created_at=MONDAY)

        async def fake_agent_executor(task, run):
            return ExecutionResult(summary="日报已生成", text_output="日报已生成")

        async def fake_delivery(task, run, result):
            return "stored"

        service = SchedulerService(
            store=store,
            agent_executor=fake_agent_executor,
            system_executor=fake_agent_executor,
            delivery=fake_delivery,
            poll_seconds=1,
            lease_seconds=300,
        )

        asyncio.run(service.run_once(now=THURSDAY))

        runs = store.list_runs(task.id)
        assert len(runs) == 1
        assert runs[0].status == "succeeded"
        assert runs[0].missed_count == 3
        assert runs[0].summary.startswith("⚠️ 本次运行前有 3 次计划未能执行")
        assert "日报已生成" in runs[0].summary
    finally:
        store.close()


def test_a_summary_with_nothing_missed_is_left_alone(tmp_path):
    import asyncio

    from agent.scheduler import ExecutionResult, SchedulerService

    store = make_store(tmp_path)
    try:
        task = add_daily_task(store, created_at=MONDAY)

        async def fake_agent_executor(task, run):
            return ExecutionResult(summary="日报已生成", text_output="日报已生成")

        async def fake_delivery(task, run, result):
            return "stored"

        service = SchedulerService(
            store=store,
            agent_executor=fake_agent_executor,
            system_executor=fake_agent_executor,
            delivery=fake_delivery,
            poll_seconds=1,
            lease_seconds=300,
        )

        asyncio.run(service.run_once(now=MONDAY_RUN))

        runs = store.list_runs(task.id)
        assert runs[0].summary == "日报已生成"
        assert runs[0].missed_count == 0
    finally:
        store.close()

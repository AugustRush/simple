"""Runs that follow each other, instead of following a clock.

A schedule can only express "at this time".  Anything with a step that depends
on another step has to be written as two schedules whose times happen to work
out, which is not the same thing and breaks the first time a run takes longer
than expected.  A signal closes that gap: a task can wait for a *named thing
that happened* and run when it does.

Generalizing the trigger this far buys real expressiveness and creates three
problems that a "previous task succeeded" trigger would not have had.  These
tests pin the answers to all three:

1. **Nothing is silently dropped.**  An emission is a row, written before
   anything is done about it, and delivery is a separate step that reads the
   rows.  A signal that arrives while the process is down is delivered when it
   comes back, and one that arrives with nobody subscribed is recorded as
   ``unmatched`` rather than vanishing into a silence indistinguishable from
   success.

2. **A cascade is bounded.**  Emitters and subscribers never mention each
   other, so the graph is never declared and cannot be checked for cycles when
   it is built -- two unrelated edits made weeks apart can assemble a ring.
   Every emission therefore carries its distance from the root, and delivery
   refuses past a ceiling.

3. **A task emits its own signal.**  Every run that reaches a terminal state
   records ``task:<id>:<status>``, in the same transaction as the status, so
   no control path can finish a run without announcing it.  Depth is inherited
   from the emission the run answered, which is what makes a chain readable
   end to end.

And the one thing that must keep working: a task waiting on a signal is never
claimed by the clock, which falls out of its having no next occurrence at all.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from agent.scheduler import (
    DEFAULT_SIGNAL_MAX_DEPTH,
    DeliveryTarget,
    ExecutionResult,
    NewScheduledTask,
    SchedulerService,
    SchedulerStore,
    TriggerSpec,
    parse_task_signal,
    task_signal_name,
)

UTC = timezone.utc
NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def tmp_path():
    """A scratch directory rooted at ``/tmp``.

    Overrides pytest's own fixture, which fails in this environment: its
    per-run root under the system temp directory cannot be created when the
    session is sandboxed, so every test that asks for ``tmp_path`` errors out
    before it runs.  ``/tmp`` is writable and just as disposable.
    """
    path = Path("/tmp") / f"simple-signals-test-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    yield path
    for child in sorted(path.rglob("*"), reverse=True):
        child.unlink() if child.is_file() else child.rmdir()
    path.rmdir()


def make_store(tmp_path: Path) -> SchedulerStore:
    return SchedulerStore(db_path=tmp_path / "scheduler.db")


def make_task(
    store: SchedulerStore,
    name: str,
    trigger: TriggerSpec,
    *,
    prompt: str = "do the thing",
    kind: str = "agent_prompt",
    enabled: bool = True,
):
    payload = {"prompt": prompt} if kind == "agent_prompt" else {"message_text": prompt}
    task = store.create_task(
        NewScheduledTask(
            name=name,
            kind=kind,
            trigger=trigger,
            payload=payload,
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
        )
    )
    if not enabled:
        store.set_enabled(task.id, False)
    return store.get_task(task.id)


def subscriber(
    store: SchedulerStore,
    name: str,
    signal: str,
    *,
    enabled: bool = True,
):
    return make_task(store, name, TriggerSpec.signal(signal), enabled=enabled)


def make_service(store: SchedulerStore, *, max_depth: int = DEFAULT_SIGNAL_MAX_DEPTH):
    async def executor(task, run):
        return ExecutionResult(summary=f"ran {task.name}", text_output=f"out {task.name}")

    async def system_executor(task, run):
        raise AssertionError("system executor should not be called")

    async def delivery(task, run, result):
        return "delivered"

    return SchedulerService(
        store=store,
        agent_executor=executor,
        system_executor=system_executor,
        delivery=delivery,
        poll_seconds=30,
        lease_seconds=300,
        signal_max_depth=max_depth,
    )


def make_due(store: SchedulerStore, task_id: str, when: datetime = NOW) -> None:
    """Move a task's next occurrence into the past so the clock can claim it."""
    store._conn.execute(
        "UPDATE scheduled_tasks SET next_run_at = ? WHERE id = ?",
        ((when - timedelta(minutes=1)).astimezone(UTC).isoformat(), task_id),
    )
    store._conn.commit()


# ── 1. A signal task is invisible to the clock ──────────────────────────────


def test_signal_task_has_no_next_occurrence_and_is_never_claimed(tmp_path):
    store = make_store(tmp_path)
    try:
        task = subscriber(store, "waits", "report.ready")

        assert task.next_run_at is None
        # Asking for its next occurrence must not raise or invent one: it has
        # no calendar, and the claim query's ``next_run_at IS NOT NULL`` is
        # what keeps that from being a special case anywhere else.
        assert task.trigger.initial_run_at(NOW) is None
        assert task.trigger.instantiate().next_after(NOW) is None

        far_future = NOW + timedelta(days=365)
        claimed = store.claim_due_tasks(
            now=far_future, limit=10, lease_seconds=300
        )
        assert claimed == []
    finally:
        store.close()


def test_signal_task_never_reports_missed_occurrences(tmp_path):
    store = make_store(tmp_path)
    try:
        task = subscriber(store, "waits", "report.ready")
        # Ten years of "the process was off" must produce no backlog: there
        # was never a moment this task was supposed to have run.
        assert task.trigger.count_missed(NOW, None) == 0
    finally:
        store.close()


def test_signal_task_still_appears_in_the_task_list(tmp_path):
    store = make_store(tmp_path)
    try:
        task = subscriber(store, "waits", "report.ready")
        listed = {item.id: item for item in store.list_tasks()}
        assert task.id in listed
        assert listed[task.id].trigger.trigger_type == "signal"
        assert listed[task.id].enabled is True
    finally:
        store.close()


# ── 2. Nothing is silently dropped ─────────────────────────────────────────


def test_emission_survives_with_nothing_running(tmp_path):
    store = make_store(tmp_path)
    try:
        emission = store.emit_signal("report.ready", {"rows": 3}, source="agent")

        assert emission.state == "pending"
        # A fresh connection is what "the process died and came back" looks
        # like from the database's point of view.
        store.close()
        store = make_store(tmp_path)
        recovered = store.get_emission(emission.id)
        assert recovered is not None
        assert recovered.state == "pending"
        assert recovered.payload == {"rows": 3}
    finally:
        store.close()


def test_pending_emission_is_delivered_when_a_subscriber_exists(tmp_path):
    store = make_store(tmp_path)
    try:
        store.emit_signal("report.ready")
        task = subscriber(store, "follows", "report.ready")

        tally = store.deliver_signals(now=NOW)

        assert tally == {"delivered": 1, "coalesced": 0, "refused": 0, "unmatched": 0}
        runs = store.list_runs(task.id)
        assert len(runs) == 1
        assert runs[0].status == "queued"
        assert runs[0].trigger_source == "signal:report.ready"
    finally:
        store.close()


def test_emission_with_no_subscriber_is_recorded_not_forgotten(tmp_path):
    store = make_store(tmp_path)
    try:
        emission = store.emit_signal("typo.ready")
        tally = store.deliver_signals(now=NOW)

        assert tally["unmatched"] == 1
        settled = store.get_emission(emission.id)
        assert settled.state == "unmatched"
        # A reason, because "no task ran" on its own is indistinguishable from
        # a task that ran and did nothing.
        assert settled.reason
    finally:
        store.close()


def test_a_near_miss_does_not_fire_and_says_so(tmp_path):
    store = make_store(tmp_path)
    try:
        subscriber(store, "follows", "report.ready")
        emission = store.emit_signal("report.raddy")

        tally = store.deliver_signals(now=NOW)

        assert tally["unmatched"] == 1
        assert store.list_runs(store.list_tasks()[0].id) == []
        assert store.get_emission(emission.id).state == "unmatched"
    finally:
        store.close()


def test_disabled_subscriber_is_not_a_subscriber(tmp_path):
    store = make_store(tmp_path)
    try:
        subscriber(store, "paused", "report.ready", enabled=False)
        emission = store.emit_signal("report.ready")

        tally = store.deliver_signals(now=NOW)

        assert tally["unmatched"] == 1
        assert store.get_emission(emission.id).state == "unmatched"
    finally:
        store.close()


def test_delivery_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    try:
        store.emit_signal("report.ready")
        task = subscriber(store, "follows", "report.ready")

        store.deliver_signals(now=NOW)
        second = store.deliver_signals(now=NOW + timedelta(seconds=1))

        # A second pass over the same rows must not queue a second run: the
        # emission leaves ``pending`` inside the transaction that queues its
        # runs, so there is no window where both could happen.
        assert second == {"delivered": 0, "coalesced": 0, "refused": 0, "unmatched": 0}
        assert len(store.list_runs(task.id)) == 1
    finally:
        store.close()


def test_one_emission_reaches_every_subscriber(tmp_path):
    store = make_store(tmp_path)
    try:
        first = subscriber(store, "first", "report.ready")
        second = subscriber(store, "second", "report.ready")
        emission = store.emit_signal("report.ready")

        tally = store.deliver_signals(now=NOW)

        # The tally counts emissions, of which there is one -- it reached two
        # tasks, and that detail is what ``signal_deliveries`` is for.
        assert tally["delivered"] == 1
        assert len(store.list_runs(first.id)) == 1
        assert len(store.list_runs(second.id)) == 1
        outcomes = {row["task_id"]: row["outcome"] for row in store.signal_deliveries(emission.id)}
        assert outcomes == {first.id: "delivered", second.id: "delivered"}
    finally:
        store.close()


def test_subscriber_that_is_already_queued_folds_in_the_signal(tmp_path):
    store = make_store(tmp_path)
    try:
        task = subscriber(store, "busy", "report.ready")
        store.emit_signal("report.ready")
        # A second emission while the first delivery's run has not started.
        second = store.emit_signal("report.ready")

        store.deliver_signals(now=NOW)
        store.emit_signal("report.ready")
        tally = store.deliver_signals(now=NOW + timedelta(seconds=1))

        # One task, one queued run -- a signal arriving during an existing run
        # must not stack up a queue of runs nobody watched being requested.
        assert len(store.list_runs(task.id)) == 1
        assert tally["coalesced"] == 1
        settled = store.get_emission(second.id)
        assert settled.state == "coalesced"
        # Recorded against the run that absorbed it, with the reason, so the
        # signal is accounted for rather than appearing to have been dropped.
        folded = store.signal_deliveries(second.id)
        assert folded[0]["outcome"] == "coalesced"
        assert folded[0]["run_id"] == store.list_runs(task.id)[0].id
        # The reason has to be honest about what folding did: the second
        # signal's payload never reaches the run that was already waiting, so
        # the wording must not read as though it had been handed over.
        reason = folded[0]["reason"]
        assert "不会送达" in reason
        assert "已记录" in reason
        assert "并入" not in reason
    finally:
        store.close()


# ── 3. A cascade is bounded ────────────────────────────────────────────────


def test_emission_past_the_depth_ceiling_is_refused_with_a_reason(tmp_path):
    store = make_store(tmp_path)
    try:
        task = subscriber(store, "follows", "loop.step")
        emission = store.emit_signal("loop.step", depth=DEFAULT_SIGNAL_MAX_DEPTH + 1)

        tally = store.deliver_signals(now=NOW)

        assert tally["refused"] == 1
        assert store.list_runs(task.id) == []
        settled = store.get_emission(emission.id)
        assert settled.state == "refused"
        assert settled.reason
        deliveries = store.signal_deliveries(emission.id)
        assert deliveries[0]["outcome"] == "refused"
        assert deliveries[0]["reason"]
    finally:
        store.close()


def test_ceiling_is_configurable_and_zero_means_first_hop_only(tmp_path):
    store = make_store(tmp_path)
    try:
        task = subscriber(store, "follows", "loop.step")

        # Depth 0 came from a person or a clock, so it is allowed even when
        # the ceiling is at its floor.
        store.emit_signal("loop.step", depth=0, source="manual")
        store.emit_signal("loop.step", depth=1, source="task")
        tally = store.deliver_signals(now=NOW, max_depth=0)

        assert tally["delivered"] == 1
        assert tally["refused"] == 1
        assert len(store.list_runs(task.id)) == 1
    finally:
        store.close()


def test_cascade_lineage_groups_every_hop_under_one_root(tmp_path):
    store = make_store(tmp_path)
    try:
        root = store.emit_signal("kickoff", source="manual")
        hop = store.emit_signal(
            "next.step", source="task", depth=1, origin_id=root.id
        )

        assert root.origin_id == root.id
        assert hop.origin_id == root.id
        same_cascade = [
            item for item in store.list_emissions() if item.origin_id == root.id
        ]
        assert {item.id for item in same_cascade} == {root.id, hop.id}
    finally:
        store.close()


def test_cascading_task_emits_one_step_further_from_the_root(tmp_path):
    store = make_store(tmp_path)
    try:
        async def scenario():
            service = make_service(store)
            inner = subscriber(store, "inner", "outer.done")

            root = store.emit_signal("outer.done", source="manual")
            await service.run_once(now=NOW)

            runs = store.list_runs(inner.id)
            assert len(runs) == 1
            signal = runs[0].config_snapshot["signal"]
            assert signal["origin_id"] == root.id
            assert signal["depth"] == 0

            own = store.list_emissions(name=task_signal_name(inner.id, "succeeded"))
            assert len(own) == 1
            # One hop out from the emission it answered, and still the same
            # cascade -- which is the whole of what makes the ceiling count.
            assert own[0].depth == 1
            assert own[0].origin_id == root.id

        asyncio.run(scenario())
    finally:
        store.close()


def test_a_ring_stops_at_the_ceiling_instead_of_running_forever(tmp_path):
    """The failure mode a declared graph would have caught and this one cannot.

    Two tasks subscribing to a signal that each of them emits is not a mistake
    anybody makes on purpose -- it is a loop that assembles itself out of two
    edits made weeks apart.  Here the executor closes the loop deliberately, so
    without a ceiling the test would not finish.
    """
    store = make_store(tmp_path)
    try:
        async def ring_executor(task, run):
            answered = (run.config_snapshot or {}).get("signal") or {}
            store.emit_signal(
                "ping",
                source="agent",
                depth=int(answered.get("depth", 0) or 0) + 1 if answered else 0,
                origin_id=str(answered.get("origin_id", "") or "") if answered else "",
            )
            return ExecutionResult(summary="pinged", text_output="ping")

        async def unused(*args, **kwargs):
            raise AssertionError("system executor should not be called")

        async def delivery(task, run, result):
            return "delivered"

        async def scenario():
            service = SchedulerService(
                store=store,
                agent_executor=ring_executor,
                system_executor=unused,
                delivery=delivery,
                poll_seconds=30,
                lease_seconds=300,
                signal_max_depth=3,
            )
            first = subscriber(store, "first", "ping")
            second = subscriber(store, "second", "ping")

            store.emit_signal("ping", source="manual")
            for step in range(12):
                await service.run_once(now=NOW + timedelta(seconds=30 * step))

            # Depths 0, 1, 2 and 3 are delivered -- four levels, two tasks --
            # and depth 4 is refused for both. Without the ceiling this loop
            # would still be running.
            assert len(store.list_runs(first.id)) == 4
            assert len(store.list_runs(second.id)) == 4
            assert all(
                run.status == "succeeded" for run in store.list_runs(first.id)
            )
            refused = [item for item in store.list_emissions() if item.state == "refused"]
            assert refused
            assert {item.depth for item in refused} == {4}
            assert not [
                item
                for item in store.list_emissions()
                if item.state == "pending"
            ]

        asyncio.run(scenario())
    finally:
        store.close()


# ── 4. A finished run announces itself ─────────────────────────────────────


def test_completed_run_emits_its_own_signal(tmp_path):
    store = make_store(tmp_path)
    try:
        from agent.scheduler import ExecutionResult as Result

        task = make_task(store, "emitter", TriggerSpec.once("2026-05-01T11:59:00+00:00", "UTC"))
        make_due(store, task.id)

        store.claim_due_tasks(now=NOW, limit=10, lease_seconds=300)
        run = store.list_runs(task.id)[0]
        store.complete_run(
            task.id,
            run.id,
            finished_at=NOW,
            status="succeeded",
            summary="all good",
        )

        emissions = store.list_emissions(name=task_signal_name(task.id, "succeeded"))
        assert len(emissions) == 1
        assert emissions[0].source == "task"
        assert emissions[0].depth == 0
        assert emissions[0].payload["task_id"] == task.id
        # The readable name travels in the payload, because the subscription
        # is keyed by id and a rename must not quietly unhook it.
        assert emissions[0].payload["task_name"] == "emitter"
        assert emissions[0].payload["status"] == "succeeded"
        assert Result is not None
    finally:
        store.close()


def test_rejected_completion_emits_nothing(tmp_path):
    store = make_store(tmp_path)
    try:
        task = make_task(store, "emitter", TriggerSpec.once("2026-05-01T11:59:00+00:00", "UTC"))
        make_due(store, task.id)
        store.claim_due_tasks(now=NOW, limit=10, lease_seconds=300)
        run = store.list_runs(task.id)[0]
        store.complete_run(task.id, run.id, finished_at=NOW, status="succeeded")

        # Completing the same run twice is refused by the ownership guard. The
        # signal must be refused with it -- otherwise a stale worker could
        # announce a success that never happened.
        assert (
            store.complete_run(task.id, run.id, finished_at=NOW, status="succeeded")
            is False
        )
        assert len(store.list_emissions(name=task_signal_name(task.id, "succeeded"))) == 1
    finally:
        store.close()


def test_failed_run_emits_failed_not_succeeded(tmp_path):
    store = make_store(tmp_path)
    try:
        task = make_task(store, "emitter", TriggerSpec.once("2026-05-01T11:59:00+00:00", "UTC"))
        make_due(store, task.id)
        store.claim_due_tasks(now=NOW, limit=10, lease_seconds=300)
        run = store.list_runs(task.id)[0]

        store.complete_run(
            task.id, run.id, finished_at=NOW, status="failed", error="boom"
        )

        assert store.list_emissions(name=task_signal_name(task.id, "failed"))
        assert store.list_emissions(name=task_signal_name(task.id, "succeeded")) == []
    finally:
        store.close()


def test_signal_triggered_run_feeds_the_next_subscriber(tmp_path):
    store = make_store(tmp_path)
    try:
        async def scenario():
            service = make_service(store)
            emitter = make_task(
                store, "emitter", TriggerSpec.once("2026-05-01T11:59:00+00:00", "UTC")
            )
            middle = subscriber(store, "middle", task_signal_name(emitter.id, "succeeded"))
            tail = subscriber(store, "tail", task_signal_name(middle.id, "succeeded"))
            make_due(store, emitter.id)

            for step in range(4):
                await service.run_once(now=NOW + timedelta(seconds=30 * step))

            assert len(store.list_runs(emitter.id)) == 1
            assert len(store.list_runs(middle.id)) == 1
            assert len(store.list_runs(tail.id)) == 1
            assert store.list_runs(tail.id)[0].status == "succeeded"
            # One cascade, three hops, one root.
            roots = {item.origin_id for item in store.list_emissions()}
            assert len(roots) == 1

        asyncio.run(scenario())
    finally:
        store.close()


def test_service_delivers_signals_before_claiming_so_a_hop_takes_one_poll(tmp_path):
    store = make_store(tmp_path)
    try:
        async def scenario():
            service = make_service(store)
            inner = subscriber(store, "inner", "go.now")
            store.emit_signal("go.now", source="agent")

            # A single iteration both delivers the pending signal and runs the
            # task it woke -- the delivery step happens before the claim, so a
            # cascade costs one poll per hop rather than two.
            await service.run_once(now=NOW)

            assert len(store.list_runs(inner.id)) == 1
            assert store.list_runs(inner.id)[0].status == "succeeded"

        asyncio.run(scenario())
    finally:
        store.close()


# ── 5. Subscriptions that cannot work are refused when written ─────────────


def test_unknown_task_id_in_a_task_signal_is_refused(tmp_path):
    store = make_store(tmp_path)
    try:
        problem = store.describe_signal_problem("task:does-not-exist:succeeded")
        assert problem
        assert "does-not-exist" in problem
    finally:
        store.close()


def test_status_a_run_cannot_reach_is_refused(tmp_path):
    store = make_store(tmp_path)
    try:
        task = make_task(store, "real", TriggerSpec.once("2026-05-01T11:59:00+00:00", "UTC"))
        problem = store.describe_signal_problem(task_signal_name(task.id, "exploded"))
        assert problem
        assert "exploded" in problem
    finally:
        store.close()


def test_a_real_task_signal_is_accepted(tmp_path):
    store = make_store(tmp_path)
    try:
        task = make_task(store, "real", TriggerSpec.once("2026-05-01T11:59:00+00:00", "UTC"))
        assert store.describe_signal_problem(task_signal_name(task.id, "succeeded")) == ""
        # A free-form name is not checked, because nothing but its emitter can
        # say whether it is spelled right -- and refusing it would make the
        # emitter and subscriber have to be created in a particular order.
        assert store.describe_signal_problem("report.ready") == ""
        assert store.describe_signal_problem("") != ""
    finally:
        store.close()


def test_parse_task_signal_round_trips(tmp_path):
    assert parse_task_signal(task_signal_name("abc123", "failed")) == ("abc123", "failed")
    assert parse_task_signal("report.ready") is None
    assert parse_task_signal("task:only-an-id") is None
    assert parse_task_signal("") is None


def test_signal_name_listing_is_what_the_picker_offers(tmp_path):
    store = make_store(tmp_path)
    try:
        store.emit_signal("report.ready", source="agent", now=NOW)
        store.emit_signal("report.ready", source="agent", now=NOW)
        task = make_task(store, "real", TriggerSpec.once("2026-05-01T11:59:00+00:00", "UTC"))
        store.claim_due_tasks(now=NOW, limit=10, lease_seconds=300)
        run = store.list_runs(task.id)[0]
        store.complete_run(
            task.id, run.id, finished_at=NOW + timedelta(minutes=5), status="succeeded"
        )

        names = {entry["name"]: entry for entry in store.signal_names()}

        assert names["report.ready"]["count"] == 2
        assert task_signal_name(task.id, "succeeded") in names
        # Newest first, so the picker puts what just happened at the top.
        assert list(names)[0] == task_signal_name(task.id, "succeeded")
    finally:
        store.close()


# ── 6. Upgrading an existing database ──────────────────────────────────────


def test_v7_database_upgrades_and_keeps_its_tasks(tmp_path):
    import sqlite3

    db_path = tmp_path / "scheduler.db"
    store = make_store(tmp_path)
    try:
        task = make_task(store, "pre-existing", TriggerSpec.daily("09:00", "UTC"))
        task_id = task.id
    finally:
        store.close()

    # Rewind to the shape the previous release left behind.
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA user_version = 7")
        connection.execute("DROP TABLE IF EXISTS signal_emissions")
        connection.execute("DROP TABLE IF EXISTS signal_deliveries")
        connection.commit()
    finally:
        connection.close()

    upgraded = make_store(tmp_path)
    try:
        assert upgraded._conn.execute("PRAGMA user_version").fetchone()[0] == 8
        assert [item.id for item in upgraded.list_tasks()] == [task_id]
        # The new tables exist and work, which is the point of the migration:
        # nothing to backfill, but the upgrade must not fail on an old file.
        emission = upgraded.emit_signal("post.upgrade")
        assert upgraded.get_emission(emission.id) is not None
    finally:
        upgraded.close()


def test_upgrade_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    store.close()
    reopened = make_store(tmp_path)
    try:
        assert reopened._conn.execute("PRAGMA user_version").fetchone()[0] == 8
        store_again = make_store(tmp_path)
        try:
            assert store_again._conn.execute("PRAGMA user_version").fetchone()[0] == 8
        finally:
            store_again.close()
    finally:
        reopened.close()


# ── 7. What a deletion is allowed to leave behind ──────────────────────────


def test_deleting_a_task_takes_its_deliveries_with_it(tmp_path):
    """A delivery names a task and a run. Both are gone when the task is.

    Leaving the row would create a record that cannot be read -- it points at
    ids nothing else knows -- and cannot be cleaned up either, since only the
    task's own deletion is a moment when anyone still knows the id.
    """
    store = make_store(tmp_path)
    try:
        task = subscriber(store, "follows", "report.ready")
        emission = store.emit_signal("report.ready")
        store.deliver_signals(now=NOW)
        assert len(store.signal_deliveries(emission.id)) == 1

        store.delete_task(task.id)

        assert store.signal_deliveries(emission.id) == []
        remaining = store._conn.execute(
            "SELECT COUNT(*) AS total FROM signal_deliveries WHERE task_id = ?",
            (task.id,),
        ).fetchone()
        assert remaining["total"] == 0
        # The emission stays. It is a record of something that happened, and
        # another subscriber's run may point back at it; deleting it would
        # rewrite that task's history to make this deletion look tidier.
        assert store.get_emission(emission.id) is not None
    finally:
        store.close()


def test_deleting_a_subscriber_does_not_disturb_the_others(tmp_path):
    store = make_store(tmp_path)
    try:
        keep = subscriber(store, "keep", "report.ready")
        drop = subscriber(store, "drop", "report.ready")
        emission = store.emit_signal("report.ready")
        store.deliver_signals(now=NOW)

        store.delete_task(drop.id)

        outcomes = {row["task_id"]: row["outcome"] for row in store.signal_deliveries(emission.id)}
        assert outcomes == {keep.id: "delivered"}
        assert len(store.list_runs(keep.id)) == 1
    finally:
        store.close()


def test_a_coalesced_signal_says_it_did_not_reach_the_run(tmp_path):
    """The wording has to match what actually happened.

    The run it is recorded against was built from an earlier emission, so this
    signal's payload never reaches it. Calling that "merged in" would be
    comfortable and false, which is the failure mode these records exist to
    remove.
    """
    store = make_store(tmp_path)
    try:
        task = subscriber(store, "busy", "report.ready")
        store.emit_signal("report.ready")
        store.deliver_signals(now=NOW)
        second = store.emit_signal("report.ready", {"note": "second"})
        store.deliver_signals(now=NOW + timedelta(seconds=1))

        delivery = store.signal_deliveries(second.id)[0]
        assert delivery["outcome"] == "coalesced"
        assert "不会送达" in delivery["reason"]
        assert store.get_emission(second.id).reason
        # And the run really does not carry it, which is what the reason says.
        run = store.list_runs(task.id)[0]
        assert run.config_snapshot["signal"]["payload"] != {"note": "second"}
    finally:
        store.close()


# ── 8. A subscriber added afterwards does not resurrect an unmatched signal ─


def test_a_subscriber_added_after_an_unmatched_signal_gets_nothing(tmp_path):
    """An emission is a record of what happened, not a queue of pending work.

    An emission with nobody waiting is closed as ``unmatched`` and stays
    closed.  Re-opening it when a subscriber appears would start a run for a
    signal emitted before anyone asked for it, which the emitter has no way to
    know about and did not ask for.  The ``emit_signal`` tool description used
    to promise the opposite -- "an emission is never lost if the subscriber is
    added later" -- which would have led a reader to emit first and subscribe
    after, an order that silently produces no run at all.  The description now
    says to subscribe first, and this pins the behaviour it describes.
    """
    store = make_store(tmp_path)
    try:
        early = store.emit_signal("report.ready")
        store.deliver_signals(now=NOW)
        assert store.get_emission(early.id).state == "unmatched"

        task = subscriber(store, "added later", "report.ready")
        for step in range(5):
            store.deliver_signals(now=NOW + timedelta(seconds=30 * step))

        assert store.list_runs(task.id) == []
        assert store.get_emission(early.id).state == "unmatched"
        # What was skipped is the ordering, not the subscription: the same
        # subscription does fire for a signal emitted after it exists.
        store.emit_signal("report.ready")
        store.deliver_signals(now=NOW + timedelta(seconds=300))
        assert len(store.list_runs(task.id)) == 1
    finally:
        store.close()


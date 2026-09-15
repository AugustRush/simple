from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner


def test_daily_trigger_next_after_returns_next_local_wall_clock_time():
    from agent.scheduler import DailyTrigger

    trigger = DailyTrigger(time_of_day="09:00", timezone_name="Asia/Shanghai")
    now = datetime(2026, 4, 19, 0, 30, tzinfo=timezone.utc)  # 08:30 local

    assert trigger.next_after(now) == datetime(
        2026, 4, 19, 1, 0, tzinfo=timezone.utc
    )


def test_weekdays_trigger_skips_weekend_in_local_timezone():
    from agent.scheduler import WeekdaysTrigger

    trigger = WeekdaysTrigger(time_of_day="09:00", timezone_name="Asia/Shanghai")
    friday_after_work = datetime(2026, 4, 17, 10, 0, tzinfo=timezone.utc)

    assert trigger.next_after(friday_after_work) == datetime(
        2026, 4, 20, 1, 0, tzinfo=timezone.utc
    )


def test_monthly_trigger_skips_month_without_requested_date():
    from agent.scheduler import MonthlyTrigger

    trigger = MonthlyTrigger(
        day_of_month=31, time_of_day="09:00", timezone_name="Asia/Shanghai"
    )
    april = datetime(2026, 4, 1, tzinfo=timezone.utc)

    assert trigger.next_after(april) == datetime(
        2026, 5, 31, 1, 0, tzinfo=timezone.utc
    )


def test_weekly_trigger_rolls_forward_to_named_weekday():
    from agent.scheduler import WeeklyTrigger

    trigger = WeeklyTrigger(
        day_of_week="wed",
        time_of_day="09:00",
        timezone_name="Asia/Shanghai",
    )
    now = datetime(2026, 4, 20, 1, 30, tzinfo=timezone.utc)  # Monday 09:30 local

    assert trigger.next_after(now) == datetime(
        2026, 4, 22, 1, 0, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    ("scheduled_for", "now", "expected"),
    [
        (
            datetime(2026, 3, 1, 14, 0, tzinfo=timezone.utc),
            datetime(2026, 3, 1, 14, 1, tzinfo=timezone.utc),
            datetime(2026, 3, 8, 13, 0, tzinfo=timezone.utc),
        ),
        (
            datetime(2026, 10, 25, 13, 0, tzinfo=timezone.utc),
            datetime(2026, 10, 25, 13, 1, tzinfo=timezone.utc),
            datetime(2026, 11, 1, 14, 0, tzinfo=timezone.utc),
        ),
    ],
)
def test_weekly_trigger_preserves_wall_clock_across_dst(
    scheduled_for, now, expected
):
    from agent.scheduler import WeeklyTrigger

    trigger = WeeklyTrigger("sun", "09:00", "America/New_York")
    next_run = trigger.advance_from(scheduled_for, now)

    assert next_run == expected
    assert next_run.astimezone(ZoneInfo("America/New_York")).hour == 9


def test_scheduler_store_creates_and_lists_tasks(tmp_path):
    from agent.scheduler import DeliveryTarget, NewScheduledTask, SchedulerStore, TriggerSpec

    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    created = store.create_task(
        NewScheduledTask(
            name="daily-summary",
            kind="agent_prompt",
            trigger=TriggerSpec.daily("09:00", "Asia/Shanghai"),
            payload={"prompt": "Summarize yesterday"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
        )
    )

    tasks = store.list_tasks()

    assert len(tasks) == 1
    assert tasks[0].id == created.id
    assert tasks[0].name == "daily-summary"
    assert tasks[0].delivery_mode == "standalone"


def test_scheduler_store_sets_schema_version(tmp_path):
    from agent.scheduler import SchedulerStore

    db_path = tmp_path / "scheduler.db"
    store = SchedulerStore(db_path=db_path)

    version = sqlite3.connect(db_path).execute("PRAGMA user_version").fetchone()[0]

    store.close()

    assert version >= 1


def test_scheduler_store_migrates_v4_permission_profile(tmp_path):
    from agent.scheduler import DeliveryTarget, NewScheduledTask, SchedulerStore, TriggerSpec

    db_path = tmp_path / "scheduler.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE scheduled_tasks (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
            enabled INTEGER NOT NULL, trigger_json TEXT NOT NULL,
            payload_json TEXT NOT NULL, delivery_mode TEXT NOT NULL,
            delivery_target_json TEXT NOT NULL, model_override TEXT,
            overlap_policy TEXT NOT NULL, missed_run_policy TEXT NOT NULL,
            workspace_root TEXT NOT NULL DEFAULT '',
            context_policy TEXT NOT NULL DEFAULT 'stateless',
            timeout_seconds INTEGER NOT NULL DEFAULT 1800,
            retry_policy_json TEXT NOT NULL DEFAULT '{}',
            selected_skills_json TEXT NOT NULL DEFAULT '[]',
            next_run_at TEXT, lease_until TEXT, active_run_id TEXT,
            last_run_at TEXT, last_success_at TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute("PRAGMA user_version = 4")
    connection.commit()
    connection.close()

    store = SchedulerStore(db_path=db_path)
    columns = {
        row[1] for row in sqlite3.connect(db_path).execute(
            "PRAGMA table_info(scheduled_tasks)"
        ).fetchall()
    }
    created = store.create_task(NewScheduledTask(
        name="migrated",
        kind="agent_prompt",
        trigger=TriggerSpec.once("2026-04-19T00:00:00+00:00", "UTC"),
        payload={"prompt": "check migration"},
        delivery_mode="standalone",
        delivery_target=DeliveryTarget.standalone(),
    ))
    store.close()

    assert "permission_profile" in columns
    assert created.permission_profile == "inherit"


def test_scheduler_store_claims_due_task_and_creates_run(tmp_path):
    from agent.scheduler import (
        DeliveryTarget,
        NewScheduledTask,
        SchedulerStore,
        TriggerSpec,
    )

    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    task = store.create_task(
        NewScheduledTask(
            name="due-task",
            kind="agent_prompt",
            trigger=TriggerSpec.once("2026-04-19T00:00:00+00:00", "UTC"),
            payload={"prompt": "Do the thing"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
        )
    )

    claimed = store.claim_due_tasks(
        now=datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc),
        limit=5,
        lease_seconds=300,
    )
    refreshed = store.get_task(task.id)
    runs = store.list_runs(task.id)

    assert len(claimed) == 1
    assert claimed[0].task.id == task.id
    assert claimed[0].run.scheduled_for == datetime(
        2026, 4, 19, 0, 0, tzinfo=timezone.utc
    )
    assert refreshed is not None
    assert refreshed.active_run_id == claimed[0].run.id
    assert refreshed.lease_until is not None
    assert len(runs) == 1
    assert runs[0].status == "running"


def test_scheduler_store_recovers_stale_run_and_requeues_task(tmp_path):
    from agent.scheduler import (
        DeliveryTarget,
        NewScheduledTask,
        SchedulerStore,
        TriggerSpec,
    )

    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    task = store.create_task(
        NewScheduledTask(
            name="stale-task",
            kind="agent_prompt",
            trigger=TriggerSpec.once("2026-04-19T00:00:00+00:00", "UTC"),
            payload={"prompt": "retry me"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
        )
    )
    claimed = store.claim_due_tasks(
        now=datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc),
        limit=5,
        lease_seconds=60,
    )

    recovered = store.recover_stale_runs(
        now=datetime(2026, 4, 19, 0, 2, tzinfo=timezone.utc)
    )
    refreshed = store.get_task(task.id)
    runs = store.list_runs(task.id)

    assert recovered == 1
    assert refreshed is not None
    assert refreshed.active_run_id is None
    assert refreshed.next_run_at == claimed[0].run.scheduled_for
    assert runs[0].status == "interrupted"


def test_scheduler_store_concurrent_claim_creates_exactly_one_run(tmp_path):
    from agent.scheduler import (
        DeliveryTarget,
        NewScheduledTask,
        SchedulerStore,
        TriggerSpec,
    )

    db_path = tmp_path / "scheduler.db"
    setup = SchedulerStore(db_path=db_path)
    task = setup.create_task(
        NewScheduledTask(
            name="single-owner",
            kind="agent_prompt",
            trigger=TriggerSpec.once("2026-04-19T00:00:00+00:00", "UTC"),
            payload={"prompt": "once"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
        )
    )
    setup.close()
    barrier = threading.Barrier(2)
    claims = []
    errors = []

    def claim():
        store = SchedulerStore(db_path=db_path)
        try:
            barrier.wait()
            claims.append(
                store.claim_due_tasks(
                    datetime(2026, 4, 19, tzinfo=timezone.utc),
                    lease_seconds=30,
                )
            )
        except Exception as exc:
            errors.append(exc)
        finally:
            store.close()

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    verify = SchedulerStore(db_path=db_path)
    assert errors == []
    assert sorted(len(items) for items in claims) == [0, 1]
    assert len(verify.list_runs(task.id)) == 1


def test_claim_due_tasks_recovers_and_reclaims_stale_run_atomically(tmp_path):
    from agent.scheduler import (
        DeliveryTarget,
        NewScheduledTask,
        SchedulerStore,
        TriggerSpec,
    )

    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    task = store.create_task(
        NewScheduledTask(
            name="recover-in-claim",
            kind="agent_prompt",
            trigger=TriggerSpec.once("2026-04-19T00:00:00+00:00", "UTC"),
            payload={"prompt": "retry"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
        )
    )
    first = store.claim_due_tasks(
        datetime(2026, 4, 19, tzinfo=timezone.utc), lease_seconds=30
    )[0]

    second = store.claim_due_tasks(
        datetime(2026, 4, 19, 0, 1, tzinfo=timezone.utc), lease_seconds=30
    )
    runs = store.list_runs(task.id)

    assert len(second) == 1
    assert second[0].run.id != first.run.id
    assert [run.status for run in runs] == ["interrupted", "running"]
    assert store.get_task(task.id).active_run_id == second[0].run.id


def test_scheduler_store_lease_and_completion_are_fenced(tmp_path):
    from agent.scheduler import (
        DeliveryTarget,
        NewScheduledTask,
        SchedulerStore,
        TriggerSpec,
    )

    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    task = store.create_task(
        NewScheduledTask(
            name="fenced",
            kind="agent_prompt",
            trigger=TriggerSpec.once("2026-04-19T00:00:00+00:00", "UTC"),
            payload={"prompt": "run"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
        )
    )
    started = datetime(2026, 4, 19, tzinfo=timezone.utc)
    claim = store.claim_due_tasks(started, lease_seconds=30)[0]
    before_expiry = datetime(2026, 4, 19, 0, 0, 20, tzinfo=timezone.utc)
    after_expiry = datetime(2026, 4, 19, 0, 0, 31, tzinfo=timezone.utc)

    assert store.renew_lease(task.id, claim.run.id, now=before_expiry, lease_seconds=30)
    assert not store.renew_lease(task.id, "stale-run", now=before_expiry, lease_seconds=30)
    assert store.owns_unexpired_lease(task.id, claim.run.id, now=before_expiry)
    assert not store.complete_run(
        task.id,
        "stale-run",
        finished_at=before_expiry,
        status="failed",
    )
    assert store.get_task(task.id).active_run_id == claim.run.id
    assert not store.renew_lease(
        task.id,
        claim.run.id,
        now=datetime(2026, 4, 19, 0, 0, 51, tzinfo=timezone.utc),
        lease_seconds=30,
    )
    assert not store.owns_unexpired_lease(task.id, claim.run.id, now=after_expiry.replace(second=51))
    assert store.complete_run(
        task.id,
        claim.run.id,
        finished_at=before_expiry,
        status="succeeded",
    )
    assert store.get_task(task.id).active_run_id is None


def test_scheduler_service_executes_due_agent_prompt_task_and_persists_run(tmp_path):
    from agent.scheduler import (
        DeliveryTarget,
        ExecutionResult,
        NewScheduledTask,
        SchedulerService,
        SchedulerStore,
        TriggerSpec,
    )

    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    task = store.create_task(
        NewScheduledTask(
            name="execute-agent",
            kind="agent_prompt",
            trigger=TriggerSpec.once("2026-04-19T00:00:00+00:00", "UTC"),
            payload={"prompt": "Write a summary"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
        )
    )

    observed = {}

    async def fake_agent_executor(task, run):
        observed["task_id"] = task.id
        observed["run_id"] = run.id
        return ExecutionResult(
            summary="done",
            text_output="summary text",
            output_path=str(tmp_path / "run.txt"),
        )

    async def fake_system_executor(task, run):
        raise AssertionError("system executor should not be called")

    async def fake_delivery(task, run, result):
        observed["delivered"] = result.summary
        return "delivered"

    service = SchedulerService(
        store=store,
        agent_executor=fake_agent_executor,
        system_executor=fake_system_executor,
        delivery=fake_delivery,
        poll_seconds=1,
        lease_seconds=300,
    )

    asyncio.run(
        service.run_once(now=datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc))
    )

    runs = store.list_runs(task.id)
    refreshed = store.get_task(task.id)

    assert observed["task_id"] == task.id
    assert observed["delivered"] == "done"
    assert runs[0].status == "succeeded"
    assert runs[0].summary == "done"
    assert refreshed is not None
    assert refreshed.next_run_at is None
    assert refreshed.active_run_id is None


def test_scheduler_run_captures_immutable_execution_snapshot(tmp_path):
    from agent.scheduler import DeliveryTarget, NewScheduledTask, SchedulerStore, TriggerSpec

    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    task = store.create_task(
        NewScheduledTask(
            name="snapshot",
            kind="agent_prompt",
            trigger=TriggerSpec.once("2026-04-19T00:00:00+00:00", "UTC"),
            payload={"prompt": "original"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
            model_override="model-a",
            workspace_root=str(tmp_path),
            context_policy="task_history",
            timeout_seconds=90,
            retry_policy={"max_attempts": 3, "backoff_seconds": 5},
            selected_skills=["quality/review"],
            permission_profile="read_only",
        )
    )
    claimed = store.claim_due_tasks(
        datetime(2026, 4, 19, tzinfo=timezone.utc), lease_seconds=30
    )[0]

    assert claimed.run.config_snapshot["payload"] == {"prompt": "original"}
    assert claimed.run.config_snapshot["model_override"] == "model-a"
    assert claimed.run.config_snapshot["workspace_root"] == str(tmp_path)
    assert claimed.run.config_snapshot["context_policy"] == "task_history"
    assert claimed.run.config_snapshot["timeout_seconds"] == 90
    assert claimed.run.config_snapshot["selected_skills"] == ["quality/review"]
    assert claimed.run.config_snapshot["permission_profile"] == "read_only"
    assert store.get_run(task.id, claimed.run.id).config_snapshot == claimed.run.config_snapshot


def test_scheduler_manual_run_does_not_consume_next_occurrence(tmp_path):
    from agent.scheduler import DeliveryTarget, NewScheduledTask, SchedulerStore, TriggerSpec

    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    task = store.create_task(
        NewScheduledTask(
            name="manual",
            kind="message",
            trigger=TriggerSpec.daily("09:00", "Asia/Shanghai"),
            payload={"message_text": "hello"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
        ),
        now=datetime(2026, 4, 18, tzinfo=timezone.utc),
    )
    original_next = task.next_run_at
    claimed = store.claim_task_now(
        task.id,
        now=datetime(2026, 4, 18, 1, tzinfo=timezone.utc),
        lease_seconds=30,
    )

    assert claimed is not None
    assert claimed.run.trigger_source == "manual"
    assert store.get_task(task.id).next_run_at == original_next


def test_scheduler_automatic_retry_is_durable_and_claimed_after_backoff(tmp_path):
    from agent.scheduler import DeliveryTarget, NewScheduledTask, SchedulerStore, TriggerSpec

    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    task = store.create_task(
        NewScheduledTask(
            name="retry",
            kind="agent_prompt",
            trigger=TriggerSpec.once("2026-04-19T00:00:00+00:00", "UTC"),
            payload={"prompt": "run"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
            retry_policy={"max_attempts": 3, "backoff_seconds": 10},
        )
    )
    started = datetime(2026, 4, 19, tzinfo=timezone.utc)
    first = store.claim_due_tasks(started, lease_seconds=30)[0]
    assert store.complete_run(
        task.id, first.run.id, finished_at=started, status="failed", error="boom"
    )
    queued = store.enqueue_retry(
        task.id, first.run.id, retry_at=started + timedelta(seconds=10)
    )
    assert queued is not None
    assert queued.status == "queued"

    store.close()
    reopened = SchedulerStore(db_path=tmp_path / "scheduler.db")
    assert reopened.claim_due_tasks(started + timedelta(seconds=9), lease_seconds=30) == []
    retry = reopened.claim_due_tasks(started + timedelta(seconds=10), lease_seconds=30)[0]
    assert retry.run.id == queued.id
    assert retry.run.attempt == 2
    assert retry.run.trigger_source == "automatic_retry"
    assert retry.run.config_snapshot == first.run.config_snapshot


def test_scheduler_service_queues_configured_retry_after_failure(tmp_path):
    from agent.scheduler import (
        DeliveryTarget,
        NewScheduledTask,
        SchedulerService,
        SchedulerStore,
        TriggerSpec,
    )

    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    task = store.create_task(
        NewScheduledTask(
            name="retry-service",
            kind="agent_prompt",
            trigger=TriggerSpec.once("2026-04-19T00:00:00+00:00", "UTC"),
            payload={"prompt": "run"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
            retry_policy={"max_attempts": 2, "backoff_seconds": 30},
        )
    )

    async def failing_executor(task, run):
        raise RuntimeError("temporary failure")

    async def unused(*args):
        raise AssertionError("unused")

    service = SchedulerService(
        store=store,
        agent_executor=failing_executor,
        system_executor=unused,
        delivery=unused,
        lease_seconds=300,
    )
    asyncio.run(
        service.run_once(now=datetime(2026, 4, 19, tzinfo=timezone.utc))
    )

    runs = store.list_runs(task.id)
    assert [run.status for run in runs] == ["failed", "queued"]
    assert runs[1].attempt == 2
    assert runs[1].retry_of_run_id == runs[0].id


def test_scheduler_run_forever_recovers_after_iteration_error(tmp_path):
    from agent.scheduler import SchedulerService, SchedulerStore

    async def unused(*args, **kwargs):
        raise AssertionError("executor should not run")

    service = SchedulerService(
        store=SchedulerStore(db_path=tmp_path / "scheduler.db"),
        agent_executor=unused,
        system_executor=unused,
        delivery=unused,
        poll_seconds=0.001,
    )
    calls = 0
    recovered = asyncio.Event()

    async def flaky_run_once(now=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient failure")
        recovered.set()
        return 0

    service.run_once = flaky_run_once

    async def scenario():
        task = asyncio.create_task(service.run_forever())
        await asyncio.wait_for(recovered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert calls >= 2


def test_scheduler_cancellation_releases_claim_committed_in_worker(tmp_path):
    from agent.scheduler import SchedulerService, SchedulerStore

    async def unused(*args, **kwargs):
        raise AssertionError("executor should not run")

    service = SchedulerService(
        store=SchedulerStore(db_path=tmp_path / "scheduler.db"),
        agent_executor=unused,
        system_executor=unused,
        delivery=unused,
    )
    claim_started = asyncio.Event()
    release_claim = asyncio.Event()
    completed: list[tuple[str, str, str]] = []
    claimed = SimpleNamespace(
        task=SimpleNamespace(id="task-1"),
        run=SimpleNamespace(id="run-1"),
    )

    async def fake_store_call(method_name, *args, **kwargs):
        if method_name == "disable_duplicate_enabled_tasks":
            return 0
        if method_name == "claim_due_tasks":
            claim_started.set()
            await release_claim.wait()
            return [claimed]
        if method_name == "release_claim":
            completed.append((args[0], args[1], "interrupted"))
            return True
        raise AssertionError(method_name)

    service._store_call = fake_store_call

    async def scenario():
        pending = asyncio.create_task(service.run_once())
        await claim_started.wait()
        pending.cancel()
        release_claim.set()
        with pytest.raises(asyncio.CancelledError):
            await pending

    asyncio.run(scenario())
    assert completed == [("task-1", "run-1", "interrupted")]


def test_release_claim_restores_scheduled_occurrence(tmp_path):
    from agent.scheduler import DeliveryTarget, NewScheduledTask, SchedulerStore, TriggerSpec

    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    scheduled_for = datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc)
    task = store.create_task(
        NewScheduledTask(
            name="retry-after-shutdown",
            kind="agent_prompt",
            trigger=TriggerSpec.once(scheduled_for.isoformat(), "UTC"),
            payload={"prompt": "run"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
        )
    )
    claim = store.claim_due_tasks(scheduled_for, lease_seconds=30)[0]

    assert store.release_claim(
        task.id,
        claim.run.id,
        now=scheduled_for,
        reason="scheduler stopped",
    )

    refreshed = store.get_task(task.id)
    runs = store.list_runs(task.id)
    assert refreshed is not None
    assert refreshed.active_run_id is None
    assert refreshed.next_run_at == scheduled_for
    assert runs[0].status == "interrupted"
    assert runs[0].error == "scheduler stopped"


def test_scheduler_service_coalesces_missed_interval_runs(tmp_path):
    from agent.scheduler import (
        DeliveryTarget,
        ExecutionResult,
        NewScheduledTask,
        SchedulerService,
        SchedulerStore,
        TriggerSpec,
    )

    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    task = store.create_task(
        NewScheduledTask(
            name="hourly-task",
            kind="agent_prompt",
            trigger=TriggerSpec.interval(
                every=1,
                unit="hours",
                anchor_at="2026-04-19T00:00:00+00:00",
                timezone_name="UTC",
            ),
            payload={"prompt": "tick"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
        )
    )

    async def fake_agent_executor(task, run):
        return ExecutionResult(summary="ok", text_output="ok")

    async def fake_system_executor(task, run):
        raise AssertionError("system executor should not be called")

    async def fake_delivery(task, run, result):
        return "stored"

    service = SchedulerService(
        store=store,
        agent_executor=fake_agent_executor,
        system_executor=fake_system_executor,
        delivery=fake_delivery,
        poll_seconds=1,
        lease_seconds=300,
    )

    asyncio.run(
        service.run_once(now=datetime(2026, 4, 19, 3, 5, tzinfo=timezone.utc))
    )

    refreshed = store.get_task(task.id)
    runs = store.list_runs(task.id)

    assert len(runs) == 1
    assert runs[0].scheduled_for == datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc)
    assert refreshed is not None
    assert refreshed.next_run_at == datetime(2026, 4, 19, 4, 0, tzinfo=timezone.utc)


def test_scheduler_service_executes_memory_tidy_system_job(tmp_path):
    from agent.scheduler import (
        DeliveryTarget,
        ExecutionResult,
        NewScheduledTask,
        SchedulerService,
        SchedulerStore,
        TriggerSpec,
    )

    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    task = store.create_task(
        NewScheduledTask(
            name="nightly-tidy",
            kind="system_job",
            trigger=TriggerSpec.once("2026-04-19T00:00:00+00:00", "UTC"),
            payload={"job_name": "memory_tidy"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
        )
    )

    async def fake_agent_executor(task, run):
        raise AssertionError("agent executor should not be called")

    async def fake_system_executor(task, run):
        return ExecutionResult(summary="tidied", text_output="memory tidied")

    async def fake_delivery(task, run, result):
        return "stored"

    service = SchedulerService(
        store=store,
        agent_executor=fake_agent_executor,
        system_executor=fake_system_executor,
        delivery=fake_delivery,
    )

    asyncio.run(
        service.run_once(now=datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc))
    )

    runs = store.list_runs(task.id)

    assert runs[0].status == "succeeded"
    assert runs[0].summary == "tidied"


def test_scheduler_service_disables_duplicate_enabled_tasks_before_running(tmp_path):
    from agent.scheduler import (
        DeliveryTarget,
        ExecutionResult,
        NewScheduledTask,
        SchedulerService,
        SchedulerStore,
        TriggerSpec,
    )

    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    spec = NewScheduledTask(
        name="memory-tidy",
        kind="system_job",
        trigger=TriggerSpec.daily("03:00", "Asia/Shanghai"),
        payload={"job_name": "memory_tidy"},
        delivery_mode="standalone",
        delivery_target=DeliveryTarget.standalone(),
    )
    first = store.create_task(spec, now=datetime(2026, 4, 19, 18, 0, tzinfo=timezone.utc))
    second = store.create_task(spec, now=datetime(2026, 4, 19, 18, 1, tzinfo=timezone.utc))
    executions = []

    async def fake_agent_executor(task, run):
        raise AssertionError("agent executor should not be called")

    async def fake_system_executor(task, run):
        executions.append(task.id)
        return ExecutionResult(summary="tidied", text_output="")

    async def fake_delivery(task, run, result):
        return "skipped"

    service = SchedulerService(
        store=store,
        agent_executor=fake_agent_executor,
        system_executor=fake_system_executor,
        delivery=fake_delivery,
    )

    claimed = asyncio.run(
        service.run_once(now=datetime(2026, 4, 19, 19, 0, tzinfo=timezone.utc))
    )

    refreshed_first = store.get_task(first.id)
    refreshed_second = store.get_task(second.id)

    assert claimed == 1
    assert executions == [first.id]
    assert refreshed_first is not None
    assert refreshed_first.enabled is True
    assert refreshed_second is not None
    assert refreshed_second.enabled is False


def test_scheduler_service_executes_due_message_task_without_agent_executor(tmp_path):
    from agent.scheduler import (
        DeliveryTarget,
        NewScheduledTask,
        SchedulerService,
        SchedulerStore,
        TriggerSpec,
    )

    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    task = store.create_task(
        NewScheduledTask(
            name="message-task",
            kind="message",
            trigger=TriggerSpec.once("2026-04-19T00:00:00+00:00", "UTC"),
            payload={"message_text": "测试一下"},
            delivery_mode="channel",
            delivery_target=DeliveryTarget.channel(
                target_type="feishu_chat",
                chat_id="oc_test_chat",
                chat_type="group",
            ),
        )
    )

    observed = {}

    async def fake_agent_executor(task, run):
        raise AssertionError("agent executor should not be called")

    async def fake_system_executor(task, run):
        raise AssertionError("system executor should not be called")

    async def fake_delivery(task, run, result):
        observed["text_output"] = result.text_output
        observed["summary"] = result.summary
        return "delivered"

    service = SchedulerService(
        store=store,
        agent_executor=fake_agent_executor,
        system_executor=fake_system_executor,
        delivery=fake_delivery,
    )

    asyncio.run(
        service.run_once(now=datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc))
    )

    runs = store.list_runs(task.id)

    assert observed["text_output"] == "测试一下"
    assert observed["summary"] == "测试一下"
    assert runs[0].status == "succeeded"
    assert runs[0].summary == "测试一下"


def test_scheduler_service_executes_claimed_tasks_concurrently(tmp_path):
    from agent.scheduler import (
        DeliveryTarget,
        ExecutionResult,
        NewScheduledTask,
        SchedulerService,
        SchedulerStore,
        TriggerSpec,
    )

    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    first = store.create_task(
        NewScheduledTask(
            name="first",
            kind="agent_prompt",
            trigger=TriggerSpec.once("2026-04-19T00:00:00+00:00", "UTC"),
            payload={"prompt": "first"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
        )
    )
    second = store.create_task(
        NewScheduledTask(
            name="second",
            kind="agent_prompt",
            trigger=TriggerSpec.once("2026-04-19T00:00:00+00:00", "UTC"),
            payload={"prompt": "second"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
        )
    )

    starts = {}
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    allow_finish = asyncio.Event()

    async def fake_agent_executor(task, run):
        starts[task.id] = asyncio.get_running_loop().time()
        if task.id == first.id:
            first_started.set()
            await second_started.wait()
            await allow_finish.wait()
        else:
            second_started.set()
            await first_started.wait()
            allow_finish.set()
        return ExecutionResult(summary=task.name, text_output=task.name)

    async def fake_system_executor(task, run):
        raise AssertionError("system executor should not be called")

    async def fake_delivery(task, run, result):
        return "stored"

    service = SchedulerService(
        store=store,
        agent_executor=fake_agent_executor,
        system_executor=fake_system_executor,
        delivery=fake_delivery,
        max_concurrent_runs=2,
    )

    asyncio.run(
        service.run_once(now=datetime(2026, 4, 19, 0, 0, tzinfo=timezone.utc))
    )

    runs_first = store.list_runs(first.id)
    runs_second = store.list_runs(second.id)

    assert first_started.is_set()
    assert second_started.is_set()
    assert len(starts) == 2
    assert runs_first[0].status == "succeeded"
    assert runs_second[0].status == "succeeded"


def test_scheduler_service_rejects_too_short_lease():
    from agent.scheduler import SchedulerService

    async def unused(*args):
        raise AssertionError("unused")

    with pytest.raises(ValueError, match="at least 3"):
        SchedulerService(
            store=object(),
            agent_executor=unused,
            system_executor=unused,
            delivery=unused,
            lease_seconds=2,
        )


def test_scheduler_service_cancels_execution_when_renewal_loses_ownership(monkeypatch):
    from agent.scheduler import SchedulerService

    completions = []
    delivered = []
    cancelled = asyncio.Event()

    class Store:
        def renew_lease(self, *args, **kwargs):
            return False

        def complete_run(self, *args, **kwargs):
            completions.append((args, kwargs))
            return True

    async def executor(task, run):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def delivery(*args):
        delivered.append(True)

    real_sleep = asyncio.sleep

    async def immediate_sleep(_delay):
        await real_sleep(0)

    monkeypatch.setattr("agent.scheduler.runtime.asyncio.sleep", immediate_sleep)
    service = SchedulerService(
        store=Store(),
        agent_executor=executor,
        system_executor=executor,
        delivery=delivery,
        lease_seconds=3,
    )
    task = SimpleNamespace(id="task", kind="agent_prompt")
    run = SimpleNamespace(
        id="run", started_at=datetime(2026, 4, 19, tzinfo=timezone.utc)
    )

    asyncio.run(service._execute_claimed(task, run))

    assert cancelled.is_set()
    assert delivered == []
    assert completions[0][1]["status"] == "interrupted"


def test_scheduler_service_checks_ownership_immediately_before_delivery():
    from agent.scheduler import ExecutionResult, SchedulerService

    delivered = []
    completions = []

    class Store:
        def owns_unexpired_lease(self, *args, **kwargs):
            return False

        def complete_run(self, *args, **kwargs):
            completions.append((args, kwargs))
            return True

        def renew_lease(self, *args, **kwargs):
            return True

    async def executor(task, run):
        return ExecutionResult(summary="done", text_output="payload")

    async def delivery(*args):
        delivered.append(True)

    service = SchedulerService(
        store=Store(),
        agent_executor=executor,
        system_executor=executor,
        delivery=delivery,
        lease_seconds=300,
    )
    task = SimpleNamespace(id="task", kind="agent_prompt")
    run = SimpleNamespace(
        id="run", started_at=datetime(2026, 4, 19, tzinfo=timezone.utc)
    )

    asyncio.run(service._execute_claimed(task, run))

    assert delivered == []
    assert completions[0][1]["status"] == "interrupted"


def test_scheduler_service_rechecks_ownership_after_delivery():
    from agent.scheduler import ExecutionResult, SchedulerService

    ownership = iter([True, False])
    completions = []

    class Store:
        def owns_unexpired_lease(self, *args, **kwargs):
            return next(ownership)

        def complete_run(self, *args, **kwargs):
            completions.append((args, kwargs))
            return True

        def renew_lease(self, *args, **kwargs):
            return True

    async def executor(task, run):
        return ExecutionResult(summary="done", text_output="payload")

    async def delivery(*args):
        return "delivered"

    service = SchedulerService(
        store=Store(),
        agent_executor=executor,
        system_executor=executor,
        delivery=delivery,
        lease_seconds=300,
    )
    task = SimpleNamespace(id="task", kind="agent_prompt")
    run = SimpleNamespace(
        id="run", started_at=datetime(2026, 4, 19, tzinfo=timezone.utc)
    )

    asyncio.run(service._execute_claimed(task, run))

    assert len(completions) == 1
    assert completions[0][1]["status"] == "interrupted"


def test_scheduler_delivery_failure_is_persisted_without_losing_executor_output(tmp_path):
    from agent.scheduler import (
        DeliveryResult,
        DeliveryTarget,
        ExecutionResult,
        NewScheduledTask,
        SchedulerService,
        SchedulerStore,
        TriggerSpec,
    )

    store = SchedulerStore(db_path=tmp_path / "scheduler.db")
    task = store.create_task(
        NewScheduledTask(
            name="delivery-fails",
            kind="agent_prompt",
            trigger=TriggerSpec.once("2026-04-19T00:00:00+00:00", "UTC"),
            payload={"prompt": "run"},
            delivery_mode="channel",
            delivery_target=DeliveryTarget.channel(
                target_type="feishu_chat", chat_id="oc_test", chat_type="group"
            ),
        )
    )
    output_path = str(tmp_path / "executor-output.md")

    async def executor(task, run):
        return ExecutionResult(
            summary="executed", text_output="send me", output_path=output_path
        )

    async def delivery(*args):
        return DeliveryResult(status="failed", error="Feishu unavailable")

    service = SchedulerService(
        store=store,
        agent_executor=executor,
        system_executor=executor,
        delivery=delivery,
    )
    asyncio.run(
        service.run_once(datetime(2026, 4, 19, tzinfo=timezone.utc))
    )

    run = store.list_runs(task.id)[0]
    refreshed = store.get_task(task.id)
    assert run.status == "failed"
    assert run.error == "Feishu unavailable"
    assert run.output_path == output_path
    assert run.delivery_status == "failed"
    assert refreshed.last_success_at is None


def test_scheduler_delivery_failure_uses_error_field(monkeypatch, tmp_path):
    from agent.scheduler.delivery import SchedulerDelivery

    delivery = SchedulerDelivery(cfg={}, output_root=tmp_path / "scheduler")
    attempts = []

    async def fail(*args, **kwargs):
        attempts.append(True)
        raise RuntimeError("send failed")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(delivery, "deliver_channel", fail)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    result = asyncio.run(
        delivery.deliver(
            task_id="task",
            run_id="run",
            delivery_mode="channel",
            target=SimpleNamespace(),
            text="payload",
            max_retries=2,
        )
    )

    assert len(attempts) == 3
    assert result.status == "failed"
    assert result.error == "send failed"
    # A failed notification does not un-produce the run.  The text is written
    # down before the send, so the path is reported even here -- otherwise a
    # step that failed to reach its chat would leave nothing behind at all,
    # and the row would keep a 120-character summary as its only record.
    assert result.output_path
    assert Path(result.output_path).read_text(encoding="utf-8") == "payload"


def test_scheduler_feishu_delivery_sends_to_stable_chat_target(monkeypatch, tmp_path):
    from agent.scheduler import DeliveryTarget
    from agent.scheduler.delivery import SchedulerDelivery

    sent = {}

    class _FakeSink:
        def __init__(
            self,
            client,
            receive_id_type,
            receive_id,
            reply_message_id=None,
            output_dir=None,
            streaming=True,
        ):
            sent["receive_id_type"] = receive_id_type
            sent["receive_id"] = receive_id
            sent["streaming"] = streaming

        async def _send_response_async(self, text: str):
            sent["text"] = text

        async def drain(self):
            sent["drained"] = True

    monkeypatch.setattr("agent.channels.feishu.FeishuOutputSink", _FakeSink)
    monkeypatch.setattr("agent.channels.feishu.build_feishu_client", lambda config: object())

    delivery = SchedulerDelivery(
        cfg={
            "channels": {
                "feishu": {
                    "enabled": True,
                    "app_id": "app",
                    "app_secret": "secret",
                    "streaming": False,
                }
            }
        }
    )

    status = asyncio.run(
        delivery.deliver_channel(
            target=DeliveryTarget.channel(
                target_type="feishu_chat",
                chat_id="oc_123",
                chat_type="group",
            ),
            text="scheduled result",
            output_dir=tmp_path,
        )
    )

    assert status == "delivered"
    assert sent["receive_id_type"] == "chat_id"
    assert sent["receive_id"] == "oc_123"
    assert sent["text"] == "scheduled result"
    assert sent["drained"] is True


def test_scheduler_feishu_delivery_honors_an_explicit_receive_id_type(monkeypatch, tmp_path):
    """A target written by the schedule editor carries its own answer.

    The chat_type -> receive_id_type heuristic predates the chat picker: it
    reads "p2p" and tries open_id, but every chat the picker offers came from
    the bot's chat list and only has a chat_id.  An explicit receive_id_type
    must win; targets written before the field existed keep the old guess.
    """
    from agent.scheduler import DeliveryTarget
    from agent.scheduler.delivery import SchedulerDelivery

    sent = {}

    class _FakeSink:
        def __init__(
            self,
            client,
            receive_id_type,
            receive_id,
            reply_message_id=None,
            output_dir=None,
            streaming=True,
        ):
            sent["receive_id_type"] = receive_id_type
            sent["receive_id"] = receive_id

        async def _send_response_async(self, text: str):
            sent["text"] = text

        async def drain(self):
            pass

    monkeypatch.setattr("agent.channels.feishu.FeishuOutputSink", _FakeSink)
    monkeypatch.setattr(
        "agent.channels.feishu.build_feishu_client", lambda config: object()
    )

    delivery = SchedulerDelivery(
        cfg={
            "channels": {
                "feishu": {"app_id": "app", "app_secret": "secret", "streaming": False}
            }
        }
    )

    explicit = asyncio.run(
        delivery.deliver_channel(
            target=DeliveryTarget(
                "feishu_chat",
                {"chat_id": "oc_picked", "chat_type": "p2p", "receive_id_type": "chat_id"},
            ),
            text="picked chat",
            output_dir=tmp_path,
        )
    )
    assert explicit == "delivered"
    # chat_type says "p2p", so the heuristic alone would have guessed open_id
    # and sent a chat_id to the open_id field -- a guaranteed API error.
    assert sent["receive_id_type"] == "chat_id"
    assert sent["receive_id"] == "oc_picked"

    legacy = asyncio.run(
        delivery.deliver_channel(
            target=DeliveryTarget("feishu_chat", {"chat_id": "ou_user", "chat_type": "p2p"}),
            text="legacy target",
            output_dir=tmp_path,
        )
    )
    assert legacy == "delivered"
    assert sent["receive_id_type"] == "open_id"


def test_scheduler_standalone_delivery_skips_empty_output(tmp_path):
    from agent.scheduler.delivery import SchedulerDelivery

    delivery = SchedulerDelivery(cfg={}, output_root=tmp_path / "scheduler")

    result = asyncio.run(
        delivery.deliver_standalone(
            task_id="task-1",
            run_id="run-1",
            text="",
        )
    )

    assert result.status == "skipped"
    assert result.output_path == ""
    assert not (tmp_path / "scheduler" / "task-1" / "run-1.md").exists()


def test_scheduler_standalone_delivery_writes_and_sends_nothing(tmp_path, monkeypatch):
    """``standalone`` means "write it down and stop".

    Every mode now persists before it does anything else, so the risk the
    refactor introduced is that the shared write turned the quiet mode into a
    sender -- which would start posting to a chat that the task never asked
    for, and would only be noticed by whoever received it.
    """
    from agent.scheduler.delivery import SchedulerDelivery

    delivery = SchedulerDelivery(cfg={}, output_root=tmp_path / "scheduler")
    attempts = []

    async def send(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("standalone delivery must not send")

    monkeypatch.setattr(delivery, "deliver_channel", send)

    result = asyncio.run(
        delivery.deliver(
            task_id="task-1",
            run_id="run-1",
            delivery_mode="standalone",
            target=SimpleNamespace(),
            text="the report",
        )
    )

    assert attempts == []
    assert result.status == "stored"
    assert result.output_path
    assert Path(result.output_path).read_text(encoding="utf-8") == "the report"


def test_schedule_cli_creates_daily_task(monkeypatch, tmp_path):
    import agent.shared as shared_module
    from agent.cli import app

    monkeypatch.setattr(shared_module, "AGENT_HOME", tmp_path)
    monkeypatch.setattr(shared_module, "DEFAULT_OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(shared_module, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(shared_module, "SCHEDULER_DIR", tmp_path / "tasks")
    monkeypatch.setattr(shared_module, "SCHEDULER_DB_FILE", tmp_path / "tasks" / "scheduler.db")

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "schedule",
            "daily",
            "daily-summary",
            "--time",
            "09:00",
            "--timezone",
            "Asia/Shanghai",
            "--prompt",
            "Summarize yesterday",
        ],
    )

    assert result.exit_code == 0
    assert "daily-summary" in result.stdout


def test_schedule_cli_lists_persisted_tasks(monkeypatch, tmp_path):
    import agent.shared as shared_module
    from agent.cli import app

    monkeypatch.setattr(shared_module, "AGENT_HOME", tmp_path)
    monkeypatch.setattr(shared_module, "DEFAULT_OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(shared_module, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(shared_module, "SCHEDULER_DIR", tmp_path / "tasks")
    monkeypatch.setattr(shared_module, "SCHEDULER_DB_FILE", tmp_path / "tasks" / "scheduler.db")

    runner = CliRunner()
    create = runner.invoke(
        app,
        [
            "schedule",
            "once",
            "one-shot",
            "--at",
            "2026-04-19T10:00:00+00:00",
            "--timezone",
            "UTC",
            "--prompt",
            "Ping me",
        ],
    )
    assert create.exit_code == 0

    listed = runner.invoke(app, ["schedule", "list"])

    assert listed.exit_code == 0
    assert "one-shot" in listed.stdout

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner


def test_daily_trigger_next_after_returns_next_local_wall_clock_time():
    from agent.scheduler import DailyTrigger

    trigger = DailyTrigger(time_of_day="09:00", timezone_name="Asia/Shanghai")
    now = datetime(2026, 4, 19, 0, 30, tzinfo=timezone.utc)  # 08:30 local

    assert trigger.next_after(now) == datetime(
        2026, 4, 19, 1, 0, tzinfo=timezone.utc
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

    monkeypatch.setattr("channels.feishu.FeishuOutputSink", _FakeSink)
    monkeypatch.setattr("channels.feishu.build_feishu_client", lambda config: object())

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

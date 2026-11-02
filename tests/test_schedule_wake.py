"""Starting a run *now* has to reach the scheduler that is running.

The gap these hold shut
-----------------------
``schedule_run`` claimed the row itself and returned "已经排上队了".  A scheduler
running in the same process never saw that claim, and could not: ``claim_due_tasks``
only picks up work that is *due*, and a claimed row is already ``running`` with a
lease.  So the run waited out ``lease_seconds``, was recovered as interrupted, and
only then became due again -- while the answer to the person said it had started.
The half that could have started it existed the whole time (``run_task_now``, used
by the web route); what was missing was a way for a tool call to reach the service.

What is held to what
--------------------
* with a scheduler serving this process, the run really executes -- claimed by
  the service, executed by its executor, and recorded as a run of that task;
* the reply distinguishes the three cases, because an agent reading "started"
  for a run that is only queued is the inconsistency this fixes: ``started``
  (executing here), ``woken`` (a live scheduler took the request on its own
  loop), ``queued`` (no scheduler: a gateway or ``agent scheduler`` will
  consume it);
* waking is one early tick, not a second polling loop;
* absent means "no scheduler in this process" and is answered with the queue,
  not with an exception.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _isolate_scheduler_state(monkeypatch, tmp_path):
    """Every test gets its own scheduler database and output directory."""
    import agent.shared as shared_module

    agent_home = tmp_path / ".agent"
    monkeypatch.setattr(shared_module, "AGENT_HOME", agent_home)
    monkeypatch.setattr(shared_module, "DEFAULT_OUTPUT_DIR", agent_home / "output")
    monkeypatch.setattr(shared_module, "SCHEDULER_DIR", agent_home / "tasks")
    monkeypatch.setattr(
        shared_module,
        "SCHEDULER_DB_FILE",
        agent_home / "tasks" / "scheduler.db",
    )


def _soon(hours: int = 2) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def make_tools(tmp_path):
    from agent import BuiltinTools, MemoryPalace, ToolRegistry

    registry = ToolRegistry()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tools = BuiltinTools(
        memory=MemoryPalace(
            base_dir=tmp_path / "memory", context_dir=tmp_path / "context"
        ),
        registry=registry,
        workspace_root=workspace,
    )
    return tools, registry, workspace


async def _call(registry, name: str, payload: dict) -> dict:
    return json.loads(await registry.call(name, payload))


async def _create_task(registry) -> str:
    """A task that is due in two hours: only a manual run may start it now."""
    created = await _call(
        registry,
        "automation_create",
        {
            "name": "nightly",
            "action_type": "agent_task",
            "prompt": "写一份日报",
            "trigger_type": "once",
            "at": _soon(),
            "timezone_name": "Asia/Shanghai",
            "intent": "用户要每天一份日报",
        },
    )
    assert created["ok"] is True, created
    return created["task"]["id"]


def _service(store, *, agent_executor, poll_seconds: float = 30.0):
    from agent.scheduler import SchedulerService

    async def unused(*args, **kwargs):
        raise AssertionError("this executor should not be called")

    async def delivered(task, run, result):
        return "delivered"

    return SchedulerService(
        store=store,
        agent_executor=agent_executor,
        system_executor=unused,
        delivery=delivered,
        poll_seconds=poll_seconds,
        lease_seconds=300,
    )


async def _serving(service) -> None:
    """Wait until ``run_forever`` is actually waiting for work.

    The gateway creates the task before serving turns, but a test that creates
    it and calls the tool on the next line races it: a service that has not
    started serving answers ``offline``, which is the honest answer and not
    the one under test here.
    """
    for _ in range(200):
        if service._running and service._loop is not None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the scheduler never started serving")


async def _stop(poller, service) -> None:
    poller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poller
    await service.shutdown()


async def _wait_for_run(store, task_id: str, run_id: str):
    """The run row, once it has stopped being in flight.

    Awaited rather than slept on: the run that is finishing needs the loop, so
    a blocking wait here would be waiting for itself.
    """
    deadline = asyncio.get_running_loop().time() + 5
    run = store.get_run(task_id, run_id)
    while run is not None and run.status in {"queued", "running"}:
        if asyncio.get_running_loop().time() > deadline:
            break
        await asyncio.sleep(0.02)
        run = store.get_run(task_id, run_id)
    return run


# ── with a scheduler in this process ────────────────────────────────────────


def test_a_run_started_through_the_tool_really_executes(tmp_path):
    """The bug, in one assertion: the claim is served, not left to go stale.

    A run that is only claimed sits in ``running`` with nobody renewing its
    lease.  This one is executed by the service's own executor and reaches a
    terminal state, which is what "已经起跑" has to mean.
    """
    from agent.scheduler import ExecutionResult, open_store

    async def scenario():
        _tools, registry, _workspace = make_tools(tmp_path)
        task_id = await _create_task(registry)
        store = open_store()
        executed = asyncio.Event()
        observed = {}

        async def agent_executor(task, run):
            observed["run_id"] = run.id
            executed.set()
            return ExecutionResult(summary="日报写好了", text_output="")

        service = _service(store, agent_executor=agent_executor)
        # The same thing the builder does at composition time.
        registry.set_context("scheduler_wake", service.start_now)
        poller = asyncio.create_task(service.run_forever())
        try:
            await _serving(service)
            reply = await _call(
                registry,
                "schedule_run",
                {"task_id": task_id, "intent": "跑一遍看看效果"},
            )
            assert reply["ok"] is True, reply
            assert reply["outcome"] == "started", reply
            assert reply["run_id"], reply
            await asyncio.wait_for(executed.wait(), timeout=2)
            assert observed["run_id"] == reply["run_id"]
            run = await _wait_for_run(store, task_id, reply["run_id"])
            assert run is not None
            assert run.status == "succeeded", run.status
        finally:
            await _stop(poller, service)
            store.close()

    asyncio.run(scenario())


def test_a_second_call_while_a_run_is_in_flight_is_refused_once(tmp_path):
    """Two promises, one run: the refusal is the claim's, and nothing is queued twice."""
    from agent.scheduler import ExecutionResult, open_store

    async def scenario():
        _tools, registry, _workspace = make_tools(tmp_path)
        task_id = await _create_task(registry)
        store = open_store()
        release = asyncio.Event()
        first_started = asyncio.Event()

        async def agent_executor(task, run):
            first_started.set()
            await release.wait()
            return ExecutionResult(summary="done", text_output="")

        service = _service(store, agent_executor=agent_executor)
        registry.set_context("scheduler_wake", service.start_now)
        poller = asyncio.create_task(service.run_forever())
        try:
            await _serving(service)
            first = await _call(
                registry, "schedule_run", {"task_id": task_id, "intent": "先跑一次"}
            )
            assert first["ok"] is True, first
            await asyncio.wait_for(first_started.wait(), timeout=2)

            second = await _call(
                registry, "schedule_run", {"task_id": task_id, "intent": "再跑一次"}
            )
            message = str(second.get("error") or "")
            if isinstance(second.get("error"), dict):
                message = str(second["error"].get("message", ""))
            assert second["ok"] is False, second
            assert "正在运行" in message
            assert first["run_id"] in message

            assert len(store.list_runs(task_id)) == 1
        finally:
            release.set()
            await _stop(poller, service)
            store.close()

    asyncio.run(scenario())


# ── without a scheduler: the queue, said out loud ────────────────────────────


def test_without_a_scheduler_the_reply_says_queued_and_who_consumes_it(tmp_path):
    """A CLI turn has no scheduler, and the honest answer is the queue.

    Not an error, and not the word "started": the claim is durable, and the
    reply names what will pick it up.
    """
    from agent.scheduler import open_store

    async def scenario():
        _tools, registry, _workspace = make_tools(tmp_path)
        task_id = await _create_task(registry)

        reply = await _call(
            registry, "schedule_run", {"task_id": task_id, "intent": "先跑一次"}
        )

        assert reply["ok"] is True, reply
        assert reply["outcome"] == "queued", reply
        assert reply["run_id"], reply
        assert reply["trigger_source"] == "manual"
        assert "gateway" in reply["note"]
        assert "agent scheduler" in reply["note"]
        assert "schedule_runs" in reply["note"]
        # The claim really is there for whichever process consumes it.
        store = open_store()
        try:
            run = store.get_run(task_id, reply["run_id"])
            assert run is not None
            assert run.status == "running"
        finally:
            store.close()

    asyncio.run(scenario())


def test_queueing_twice_does_not_queue_a_second_run(tmp_path):
    """Repeated calls are refused by the claim, so the queue never doubles."""
    from agent.scheduler import open_store

    async def scenario():
        _tools, registry, _workspace = make_tools(tmp_path)
        task_id = await _create_task(registry)

        first = await _call(
            registry, "schedule_run", {"task_id": task_id, "intent": "跑一次"}
        )
        second = await _call(
            registry, "schedule_run", {"task_id": task_id, "intent": "又跑一次"}
        )

        assert first["ok"] is True, first
        assert first["outcome"] == "queued", first
        assert second["ok"] is False, second
        store = open_store()
        try:
            assert len(store.list_runs(task_id)) == 1
        finally:
            store.close()

    asyncio.run(scenario())


def test_a_wake_callback_that_fails_falls_back_to_the_queue(tmp_path):
    """A broken publisher is a queue, not a traceback.

    The callback is the composition's; if it raises, the run has not started, so
    the honest answer is the durable claim -- and a courtesy that failed must
    not be able to fail the tool.
    """
    from agent.scheduler import open_store

    async def broken(task_id, *, run_id=""):
        raise RuntimeError("the scheduler loop is gone")

    async def scenario():
        _tools, registry, _workspace = make_tools(tmp_path)
        task_id = await _create_task(registry)
        registry.set_context("scheduler_wake", broken)

        reply = await _call(
            registry, "schedule_run", {"task_id": task_id, "intent": "跑一次"}
        )

        assert reply["ok"] is True, reply
        assert reply["outcome"] == "queued", reply
        assert reply["run_id"], reply
        store = open_store()
        try:
            assert len(store.list_runs(task_id)) == 1
        finally:
            store.close()

    asyncio.run(scenario())


# ── waking is one early tick, not a second loop ─────────────────────────────


def test_wake_makes_the_next_poll_happen_now_without_busy_waiting(tmp_path):
    """The interval is the fallback for "is anything due", not the latency floor.

    A 30s poll that is woken has to tick at once; and it has to go back to
    waiting afterwards.  A wake that is never consumed -- or consumed twice --
    turns the loop into a busy wait, and this asserts both halves from a
    worker thread, which is where a tool used to call from.
    """
    from agent.scheduler import open_store

    async def scenario():
        service = _service(open_store(), agent_executor=_unreachable_executor)
        second_tick = asyncio.Event()
        calls = {"n": 0}

        async def counted(now=None, *, background=False):
            calls["n"] += 1
            if calls["n"] >= 2:
                second_tick.set()
            return 0

        service.run_once = counted
        poller = asyncio.create_task(service.run_forever())
        try:
            for _ in range(200):
                if calls["n"] >= 1:
                    break
                await asyncio.sleep(0.01)
            assert calls["n"] == 1
            await asyncio.sleep(0.1)
            assert calls["n"] == 1, "a 30s poll must not tick again on its own"

            # From a real thread: the sync-tool context `schedule_run` ran in.
            assert await asyncio.to_thread(service.wake) is True
            await asyncio.wait_for(second_tick.wait(), timeout=2)
            await asyncio.sleep(0.1)
            assert calls["n"] == 2, "one wake, one tick -- not a loop"
        finally:
            await _stop(poller, service)
            service.store.close()

    asyncio.run(scenario())


def test_a_scheduler_that_is_not_serving_cannot_be_woken(tmp_path):
    """Built but not started is not running, and the service says so.

    The tool layer reads ``offline`` as "queue it", so this is the difference
    between an honest queue and a wake that nothing is waiting for.
    """
    from agent.scheduler import open_store

    store = open_store()
    try:
        service = _service(store, agent_executor=_unreachable_executor)
        assert service.wake() is False
        assert asyncio.run(service.start_now("some-task"))["outcome"] == "offline"
    finally:
        store.close()


# ── a scheduler on another loop: the request is handed over ─────────────────


def test_a_scheduler_on_another_loop_is_handed_the_request(tmp_path):
    """Live, but elsewhere: claim on its own loop rather than claim here.

    ``run_task_now`` creates the execution task on the loop it runs on, so a
    request from another loop has to be handed over.  The reply says ``woken``
    because the run row does not exist yet -- and it does get created, which is
    the half a "started" would have been guessing about.
    """
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
            name="nightly",
            kind="agent_prompt",
            trigger=TriggerSpec.once(_soon(), "UTC"),
            payload={"prompt": "run"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
        )
    )
    executed = threading.Event()

    async def agent_executor(task, run):
        executed.set()
        return ExecutionResult(summary="done", text_output="")

    async def unused(*args, **kwargs):
        raise AssertionError("not used")

    async def delivered(task, run, result):
        return "delivered"

    service = SchedulerService(
        store=store,
        agent_executor=agent_executor,
        system_executor=unused,
        delivery=delivered,
        poll_seconds=30,
        lease_seconds=300,
    )

    loop = asyncio.new_event_loop()
    running = {"task": None}

    def serve():
        asyncio.set_event_loop(loop)

        async def main():
            running["task"] = asyncio.current_task()
            await service.run_forever()

        try:
            loop.run_until_complete(main())
        except asyncio.CancelledError:
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        for _ in range(500):
            if service._running and service._loop is loop:
                break
            threading.Event().wait(0.01)
        assert service._running, "the scheduler never started serving"

        outcome = asyncio.run(service.start_now(task.id))
        assert outcome["outcome"] == "woken", outcome
        assert executed.wait(5), "the handed-over request never reached the executor"

        async def settle():
            return await _wait_for_run(
                store, task.id, store.list_runs(task.id)[0].id
            )

        run = asyncio.run(settle())
        assert run is not None and run.status == "succeeded"
    finally:
        if running["task"] is not None:
            loop.call_soon_threadsafe(running["task"].cancel)
        thread.join(timeout=5)
        loop.close()
        store.close()


# ── the composition publishes the callback ─────────────────────────────────


def test_the_builder_publishes_the_wake_callback(tmp_path):
    """The one place that has both the service and the registry publishes it.

    ``schedule_run`` must not know what a ``SchedulerService`` is; it looks the
    callback up by name, the same shape ``runtime_refresh`` uses.
    """
    from agent import ToolRegistry
    from agent.scheduler import open_store
    from agent.scheduler.build import build_scheduler_service

    registry = ToolRegistry()
    store = open_store()
    try:
        service, built = build_scheduler_service(
            {},
            poll_seconds=1,
            lease_seconds=300,
            max_concurrent_runs=1,
            memory=object(),
            output_dir=tmp_path / "out",
            registry=registry,
            store=store,
        )
        assert built is store
        assert registry.get_context("scheduler_wake") == service.start_now
        # A session sees it through the fork it was built with, which is how a
        # conversation's turn reaches the scheduler.
        assert registry.fork().get_context("scheduler_wake") == service.start_now
    finally:
        store.close()


def test_a_composition_without_a_registry_publishes_nothing_and_does_not_fail(
    tmp_path,
):
    """The builder tolerates no registry, which is what a bare script has."""
    from agent.scheduler import open_store
    from agent.scheduler.build import build_scheduler_service

    store = open_store()
    try:
        service, _built = build_scheduler_service(
            {},
            poll_seconds=1,
            lease_seconds=300,
            max_concurrent_runs=1,
            memory=object(),
            output_dir=tmp_path / "out",
            registry=None,
            store=store,
        )
        assert service is not None
    finally:
        store.close()


async def _unreachable_executor(*args, **kwargs):
    raise AssertionError("no run should reach an executor here")


# ── creation says which clock will serve the task ────────────────────────────


def test_creating_a_task_where_nothing_schedules_says_so(tmp_path):
    """The cheapest moment to say "this will not run here" is while the asker is here.

    A task created in a single-turn CLI is stored correctly and will never start
    until some other process serves it.  "Created" reads as "will run", so the reply
    carries the same signal `schedule_run` uses: a live scheduler publishes itself
    into the registry context, and absence means "no clock in this process" rather
    than a missing writer.
    """
    tools, registry, _ = make_tools(tmp_path)

    created = asyncio.run(_call(registry, "automation_create", {
        "name": "nightly",
        "action_type": "agent_task",
        "prompt": "写一份日报",
        "trigger_type": "once",
        "at": _soon(),
        "timezone_name": "Asia/Shanghai",
        "intent": "用户要每天一份日报",
    }))

    assert created["ok"] is True, created
    assert created["task"]["runner"] == "external"
    assert "gateway" in created["summary_text"], created["summary_text"]
    assert "agent scheduler" in created["summary_text"], created["summary_text"]


def test_creating_a_task_under_a_live_scheduler_does_not_hedge(tmp_path):
    """Control: saying "someone else will run it" when we are the someone is noise."""
    tools, registry, _ = make_tools(tmp_path)
    registry.set_context("scheduler_wake", lambda *a, **k: None)

    created = asyncio.run(_call(registry, "automation_create", {
        "name": "nightly",
        "action_type": "agent_task",
        "prompt": "写一份日报",
        "trigger_type": "once",
        "at": _soon(),
        "timezone_name": "Asia/Shanghai",
        "intent": "用户要每天一份日报",
    }))

    assert created["task"]["runner"] == "this-process"
    assert "gateway" not in created["summary_text"], created["summary_text"]

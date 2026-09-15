"""Steps that depend on other steps, declared instead of inferred.

Signals alone can make one task follow another, and that was enough to prove
the mechanism.  It is not enough to *design* a chain: a step's edges live in
the names of the signals its upstreams happen to emit, so the graph exists
only in the reader's head, and nothing can answer "what else was supposed to
run" when a step fails.

A workflow stores the graph.  Two consequences are the whole point of this
file:

1. **A step is a task that already exists.**  The entry steps of a workflow are
   ordinary scheduled tasks with whatever trigger they were given -- a clock,
   an external signal -- and every other step is triggered by its upstreams
   finishing.  Nothing new had to be invented to run a step; what is new is
   where its trigger comes from.

2. **The graph can be checked before it runs.**  A cycle, an upstream that does
   not exist, a step whose edges and trigger disagree about when it runs: all
   of those are refused while somebody is still looking at the screen, instead
   of becoming a task that quietly never fires.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.scheduler import (
    DeliveryResult,
    DeliveryTarget,
    ExecutionResult,
    NewScheduledTask,
    RUN_SKIPPED_STATUS,
    RUN_SUCCESS_STATUS,
    SchedulerService,
    SchedulerStore,
    TriggerSpec,
    Workflow,
    WorkflowStep,
    parse_task_signal,
    signal_mode,
    signal_names,
    task_signal_name,
    validate_workflow_graph,
    workflow_downstream_steps,
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
    path = Path("/tmp") / f"simple-workflows-test-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    yield path
    for child in sorted(path.rglob("*"), reverse=True):
        child.unlink() if child.is_file() else child.rmdir()
    path.rmdir()


def make_store(tmp_path: Path) -> SchedulerStore:
    return SchedulerStore(db_path=tmp_path / "scheduler.db")


def step(
    key: str,
    *,
    name: str = "",
    depends_on: list[str] | None = None,
    trigger: TriggerSpec | None = None,
    kind: str = "agent_prompt",
    payload: dict | None = None,
    workspace_root: str = "",
    timeout_seconds: int = 1800,
    delivery_mode: str = "standalone",
) -> WorkflowStep:
    body = payload
    if body is None:
        body = (
            {"prompt": f"do {key}"} if kind == "agent_prompt" else {"message_text": key}
        )
    return WorkflowStep(
        key=key,
        name=name or key,
        kind=kind,
        payload=body,
        depends_on=list(depends_on or []),
        trigger=trigger,
        workspace_root=workspace_root,
        timeout_seconds=timeout_seconds,
        delivery_mode=delivery_mode,
    )


def clock(at: str = "2026-05-01T11:59:00+00:00") -> TriggerSpec:
    return TriggerSpec.once(at, "UTC")


def make_service(
    store: SchedulerStore, failing: set[str] | None = None
) -> SchedulerService:
    """A service whose ``agent_prompt`` steps succeed, except the named ones.

    Failure is injected by step *name* rather than by patching the store, so
    the test drives the same path a real failure takes: the runtime calls
    ``complete_run`` with ``failed``, and everything downstream of that has to
    happen on its own.
    """
    broken = {str(item) for item in (failing or set())}

    async def executor(task, run):
        if task.name in broken:
            raise RuntimeError(f"{task.name} blew up")
        return ExecutionResult(
            summary=f"ran {task.name}", text_output=f"out {task.name}"
        )

    async def unused(*args, **kwargs):
        raise AssertionError("system executor should not be called")

    async def delivery(task, run, result):
        return "delivered"

    return SchedulerService(
        store=store,
        agent_executor=executor,
        system_executor=unused,
        delivery=delivery,
        poll_seconds=30,
        lease_seconds=300,
    )


def make_due(store: SchedulerStore, task_id: str, when: datetime = NOW) -> None:
    """Move a task's next occurrence into the past so the clock can claim it."""
    store._conn.execute(
        "UPDATE scheduled_tasks SET next_run_at = ? WHERE id = ?",
        ((when - timedelta(minutes=1)).astimezone(UTC).isoformat(), task_id),
    )
    store._conn.commit()


def linear_workflow() -> Workflow:
    return Workflow(
        name="nightly report",
        steps=[
            step("collect", trigger=clock()),
            step("analyze", depends_on=["collect"]),
            step("publish", depends_on=["analyze"]),
        ],
    )


def fork_workflow() -> Workflow:
    return Workflow(
        name="fork",
        steps=[
            step("start", trigger=clock()),
            step("left", depends_on=["start"]),
            step("right", depends_on=["start"]),
            step("join", depends_on=["left", "right"]),
        ],
    )


# ── 1. A graph becomes tasks ───────────────────────────────────────────────


def test_a_workflow_materializes_one_task_per_step(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())

        tasks = store.step_tasks(workflow.id)
        assert sorted(tasks) == ["analyze", "collect", "publish"]
        assert all(task.workflow_id == workflow.id for task in tasks.values())
        assert all(task.step_key for task in tasks.values())
        # Every step is a real task in the ordinary table -- nothing about a
        # step is special to the store, so everything that already works for a
        # task (history, retries, attention) works for a step.
        assert len(store.list_tasks()) == 3
    finally:
        store.close()


def test_an_entry_step_keeps_the_trigger_it_was_given(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        entry = store.step_tasks(workflow.id)["collect"]

        assert entry.trigger.trigger_type == "once"
        # And it is a clock task, so the claim path can actually pick it up.
        assert entry.next_run_at is not None
    finally:
        store.close()


def test_a_dependent_step_waits_for_its_upstreams_success(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)

        analyze = tasks["analyze"]
        assert analyze.trigger.trigger_type == "signal"
        assert signal_mode(analyze.trigger) == "all"
        assert signal_names(analyze.trigger) == [
            task_signal_name(tasks["collect"].id, RUN_SUCCESS_STATUS)
        ]
        # A step that follows another is never claimed by the clock, which is
        # the same property a signal task has always had.
        assert analyze.next_run_at is None

        publish = tasks["publish"]
        assert signal_names(publish.trigger) == [
            task_signal_name(analyze.id, RUN_SUCCESS_STATUS)
        ]
    finally:
        store.close()


def test_a_step_waits_for_all_of_a_fork_not_just_one(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(fork_workflow())
        tasks = store.step_tasks(workflow.id)
        assert sorted(signal_names(tasks["join"].trigger)) == sorted(
            [
                task_signal_name(tasks["left"].id, RUN_SUCCESS_STATUS),
                task_signal_name(tasks["right"].id, RUN_SUCCESS_STATUS),
            ]
        )
        # Materialized in dependency order, which is what let the join's
        # trigger name ids that had to exist first.
        assert store.materialize_workflow(workflow.id)["order"] == [
            "start",
            "left",
            "right",
            "join",
        ]
    finally:
        store.close()


# ── 2. A bad graph is refused where it can still be read ───────────────────


def test_a_cyclic_graph_is_refused_and_names_the_ring():
    steps = [
        step("a", trigger=clock()),
        step("b", depends_on=["a"]),
        step("c", depends_on=["b"]),
        step("a2", depends_on=["c"]),
    ]
    steps[0].depends_on = ["a2"]
    with pytest.raises(ValueError) as error:
        validate_workflow_graph(steps)
    message = str(error.value)
    assert "循环依赖" in message
    # The ring, not just the fact of one: "which steps" is the actionable part.
    assert "a → a2 → c → b → a" in message


def test_an_upstream_that_does_not_exist_is_refused():
    with pytest.raises(ValueError) as error:
        validate_workflow_graph(
            [step("collect", trigger=clock()), step("analyze", depends_on=["colect"])]
        )
    assert "colect" in str(error.value)


def test_a_step_cannot_depend_on_itself():
    with pytest.raises(ValueError) as error:
        validate_workflow_graph([step("a", trigger=clock(), depends_on=["a"])])
    assert "不能依赖自己" in str(error.value)


def test_a_step_may_not_name_its_upstream_twice():
    with pytest.raises(ValueError) as error:
        validate_workflow_graph(
            [
                step("collect", trigger=clock()),
                step("analyze", depends_on=["collect", "collect"]),
            ]
        )
    assert "重复声明了上游" in str(error.value)


def test_an_entry_step_without_a_trigger_is_refused():
    with pytest.raises(ValueError) as error:
        validate_workflow_graph([step("collect")])
    assert "必须自带触发方式" in str(error.value)


def test_a_dependent_step_may_not_also_carry_a_clock():
    """Two answers to "when does this run" is one answer too many."""
    with pytest.raises(ValueError) as error:
        validate_workflow_graph(
            [
                step("collect", trigger=clock()),
                step("analyze", depends_on=["collect"], trigger=clock()),
            ]
        )
    assert "触发方式由上游决定" in str(error.value)


def test_duplicate_step_keys_are_refused():
    with pytest.raises(ValueError) as error:
        validate_workflow_graph([step("collect", trigger=clock()), step("collect")])
    assert "重复" in str(error.value)


def test_an_empty_workflow_is_refused():
    with pytest.raises(ValueError) as error:
        validate_workflow_graph([])
    assert "至少要有一个步骤" in str(error.value)


def test_a_step_with_no_work_in_it_is_refused():
    with pytest.raises(ValueError) as error:
        validate_workflow_graph([step("collect", trigger=clock(), payload={})])
    assert "缺少 prompt" in str(error.value)


def test_an_unsupported_step_kind_is_refused():
    with pytest.raises(ValueError) as error:
        validate_workflow_graph(
            [step("collect", trigger=clock(), kind="shell_script", payload={"x": 1})]
        )
    assert "执行类型不支持" in str(error.value)


def test_a_step_with_a_non_positive_timeout_is_refused():
    with pytest.raises(ValueError) as error:
        validate_workflow_graph([step("collect", trigger=clock(), timeout_seconds=0)])
    assert "超时时间必须为正数" in str(error.value)


def test_a_workflow_is_stored_with_its_graph(tmp_path):
    store = make_store(tmp_path)
    try:
        stored = store.create_workflow(linear_workflow())
        reloaded = store.get_workflow(stored.id)
        assert [item.key for item in reloaded.steps] == [
            "collect",
            "analyze",
            "publish",
        ]
        assert reloaded.steps[1].depends_on == ["collect"]
        assert store.list_workflows()[0].id == stored.id
    finally:
        store.close()


def test_a_refused_graph_never_becomes_tasks(tmp_path):
    store = make_store(tmp_path)
    try:
        with pytest.raises(ValueError):
            store.create_workflow(
                Workflow(
                    name="broken",
                    steps=[
                        step("collect", trigger=clock()),
                        step("analyze", depends_on=["colect"]),
                    ],
                )
            )
        # Nothing half-built is left behind for a later run to trip over.
        assert store.list_tasks() == []
        assert store.list_workflows() == []
    finally:
        store.close()


# ── 3. Editing a graph does not rewire what it did not touch ───────────────


def test_re_materializing_keeps_the_task_ids(tmp_path):
    """Task ids are what the downstream subscriptions point at."""
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        before = {key: task.id for key, task in store.step_tasks(workflow.id).items()}

        store.materialize_workflow(workflow.id)
        after = {key: task.id for key, task in store.step_tasks(workflow.id).items()}

        assert before == after
        assert len(store.list_tasks()) == 3
    finally:
        store.close()


def test_adding_a_step_leaves_the_edges_above_it_alone(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        original = store.step_tasks(workflow.id)

        edited = store.get_workflow(workflow.id)
        edited.steps.insert(2, step("review", depends_on=["analyze"]))
        edited.steps[3] = step("publish", depends_on=["review"])
        store.update_workflow(workflow.id, edited)

        tasks = store.step_tasks(workflow.id)
        assert tasks["collect"].id == original["collect"].id
        assert tasks["analyze"].id == original["analyze"].id
        # publish is the same step and the same task, rewired to the new step.
        assert tasks["publish"].id == original["publish"].id
        assert signal_names(tasks["publish"].trigger) == [
            task_signal_name(tasks["review"].id, RUN_SUCCESS_STATUS)
        ]
    finally:
        store.close()


def test_a_step_removed_from_the_graph_stops_firing(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())

        edited = store.get_workflow(workflow.id)
        edited.steps = [item for item in edited.steps if item.key != "analyze"]
        edited.steps[1] = step("publish", depends_on=["collect"])
        store.update_workflow(workflow.id, edited)

        # The report is the materialization's answer, and it names the step
        # that left so a caller can say which one it was.
        assert store.materialize_workflow(workflow.id)["removed"] == ["analyze"]
        # Its row and its history survive; what goes is its ability to fire.
        # A step being removed is not the same thing as the record of what it
        # did being wrong.
        all_tasks = {task.name: task for task in store.list_tasks()}
        assert all_tasks["analyze"].enabled is False
        assert all_tasks["publish"].enabled is True
        assert signal_names(all_tasks["publish"].trigger) == [
            task_signal_name(all_tasks["collect"].id, RUN_SUCCESS_STATUS)
        ]
    finally:
        store.close()


def test_disabling_a_workflow_disables_every_step(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        disabled = store.get_workflow(workflow.id)
        disabled.enabled = False
        store.update_workflow(workflow.id, disabled)

        assert all(
            task.enabled is False for task in store.step_tasks(workflow.id).values()
        )
    finally:
        store.close()


def test_deleting_a_workflow_keeps_what_it_ran(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        store.claim_due_tasks(now=NOW, limit=10, lease_seconds=300)

        ids = store.delete_workflow(workflow.id)

        assert store.get_workflow(workflow.id) is None
        assert sorted(ids) == sorted(task.id for task in tasks.values())
        assert all(store.get_task(task_id).enabled is False for task_id in ids)
        # History is not tidied away with the graph.
        assert store.list_runs(tasks["collect"].id)
    finally:
        store.close()


def test_a_step_without_a_folder_inherits_the_entry_steps(tmp_path):
    """A chain is one job; half of it running elsewhere is a silent split."""
    store = make_store(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    try:
        workflow = store.create_workflow(
            Workflow(
                name="job",
                steps=[
                    step("collect", trigger=clock(), workspace_root=str(project)),
                    step("analyze", depends_on=["collect"]),
                ],
            )
        )
        report = store.materialize_workflow(workflow.id)
        tasks = store.step_tasks(workflow.id)

        assert tasks["analyze"].workspace_root == str(project.resolve())
        # And the report says so, because an inheritance nobody can see is
        # indistinguishable from a step about to run in the wrong place.
        assert report["inherited_workspace"] == ["analyze"]
    finally:
        store.close()


def test_a_step_that_chose_its_own_folder_keeps_it(tmp_path):
    store = make_store(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    try:
        workflow = store.create_workflow(
            Workflow(
                name="job",
                steps=[
                    step("collect", trigger=clock()),
                    step("analyze", depends_on=["collect"], workspace_root=str(other)),
                ],
            )
        )
        assert store.step_tasks(workflow.id)["analyze"].workspace_root == str(
            other.resolve()
        )
    finally:
        store.close()


# ── 4. The graph runs ──────────────────────────────────────────────────────


def test_a_fork_runs_each_step_once_and_the_join_once(tmp_path):
    """The whole point, end to end, through the real runtime."""
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(fork_workflow())
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["start"].id)

        async def scenario():
            service = make_service(store)
            for hop in range(6):
                await service.run_once(now=NOW + timedelta(seconds=30 * hop))

        asyncio.run(scenario())

        for key in ("start", "left", "right", "join"):
            runs = store.list_runs(tasks[key].id)
            assert len(runs) == 1, f"{key} ran {len(runs)} times"
            assert runs[0].status == "succeeded"
        # The join waits for both arms, so it runs on a signal, not on a clock.
        join_run = store.list_runs(tasks["join"].id)[0]
        assert join_run.trigger_source.startswith("signal:")
    finally:
        store.close()


def test_a_second_round_of_the_fork_runs_the_join_again_exactly_once(tmp_path):
    """A join fires once per round, whatever order the arms finish in."""
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(fork_workflow())
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["start"].id)

        async def scenario():
            service = make_service(store)
            for hop in range(6):
                await service.run_once(now=NOW + timedelta(seconds=30 * hop))
            # A second turn of the crank, started by hand so the entry step
            # does not have to be re-armed.
            store.emit_signal(
                task_signal_name(tasks["start"].id, RUN_SUCCESS_STATUS),
                source="manual",
            )
            for hop in range(6, 14):
                await service.run_once(now=NOW + timedelta(seconds=30 * hop))

        asyncio.run(scenario())

        assert len(store.list_runs(tasks["left"].id)) == 2
        assert len(store.list_runs(tasks["right"].id)) == 2
        # Exactly one more round: not two, and not zero.
        assert len(store.list_runs(tasks["join"].id)) == 2
    finally:
        store.close()


# ── 5. What the graph is for: knowing what a failure blocks ────────────────


def test_the_steps_below_a_failure_are_the_whole_subtree():
    steps = [
        step("start", trigger=clock()),
        step("left", depends_on=["start"]),
        step("right", depends_on=["start"]),
        step("join", depends_on=["left", "right"]),
        step("publish", depends_on=["join"]),
        step("unrelated", trigger=clock()),
    ]
    assert sorted(workflow_downstream_steps(steps, "left")) == ["join", "publish"]
    assert sorted(workflow_downstream_steps(steps, "start")) == [
        "join",
        "left",
        "publish",
        "right",
    ]
    # A step with nothing below it blocks nothing, and an unrelated entry step
    # is not dragged in by a graph that does not reach it.
    assert workflow_downstream_steps(steps, "publish") == []
    assert workflow_downstream_steps(steps, "unrelated") == []


def test_the_blocked_steps_come_back_in_graph_order(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(fork_workflow())
        blocked = store.workflow_blocked_steps(workflow.id, "left")
        assert [key for key, _ in blocked] == ["join"]
        assert store.workflow_blocked_steps(workflow.id, "join") == []
    finally:
        store.close()


# ── 5b. A failure settles what it blocks instead of leaving it waiting ─────


def test_a_failed_step_skips_the_step_below_it(tmp_path):
    """The one failure mode a chain has that a lone task does not.

    A step waits for its upstreams to *succeed*.  If one of them fails, that
    success is never coming, so the step would wait for ever -- and in silence,
    with no run and no error, looking exactly like a step nobody has gotten
    around to running yet.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["collect"].id)

        async def scenario():
            service = make_service(store, failing={"analyze"})
            for hop in range(6):
                await service.run_once(now=NOW + timedelta(seconds=30 * hop))

        asyncio.run(scenario())

        analyze_runs = store.list_runs(tasks["analyze"].id)
        assert [run.status for run in analyze_runs] == ["failed"]
        publish_runs = store.list_runs(tasks["publish"].id)
        assert [run.status for run in publish_runs] == ["skipped"]
        assert "analyze" in publish_runs[0].summary
    finally:
        store.close()


def test_a_skip_reaches_the_whole_subtree_not_just_the_next_step(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(
            Workflow(
                name="deep",
                steps=[
                    step("collect", trigger=clock()),
                    step("analyze", depends_on=["collect"]),
                    step("review", depends_on=["analyze"]),
                    step("publish", depends_on=["review"]),
                ],
            )
        )
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["collect"].id)

        async def scenario():
            service = make_service(store, failing={"analyze"})
            for hop in range(6):
                await service.run_once(now=NOW + timedelta(seconds=30 * hop))

        asyncio.run(scenario())

        assert [run.status for run in store.list_runs(tasks["analyze"].id)] == ["failed"]
        # Both of the steps below it, not only the adjacent one.
        assert [run.status for run in store.list_runs(tasks["review"].id)] == ["skipped"]
        assert [run.status for run in store.list_runs(tasks["publish"].id)] == [
            "skipped"
        ]
    finally:
        store.close()


def test_a_skip_says_which_step_blocked_it(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["collect"].id)

        async def scenario():
            service = make_service(store, failing={"collect"})
            for hop in range(6):
                await service.run_once(now=NOW + timedelta(seconds=30 * hop))

        asyncio.run(scenario())

        for key in ("analyze", "publish"):
            runs = store.list_runs(tasks[key].id)
            assert [run.status for run in runs] == ["skipped"]
            assert "collect" in runs[0].summary
            assert runs[0].trigger_source == "workflow:collect"
    finally:
        store.close()


def test_a_skipped_step_announces_itself_like_any_other_terminal_run(tmp_path):
    """A status that exists is a signal that exists -- skips included."""
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["collect"].id)

        async def scenario():
            service = make_service(store, failing={"analyze"})
            for hop in range(6):
                await service.run_once(now=NOW + timedelta(seconds=30 * hop))

        asyncio.run(scenario())

        names = {item.name for item in store.list_emissions()}
        assert task_signal_name(tasks["analyze"].id, "failed") in names
        assert task_signal_name(tasks["publish"].id, RUN_SKIPPED_STATUS) in names
        # And the status is one a person can actually subscribe to, which is
        # what makes "clean up after this step was skipped" sayable.
        assert (
            store.describe_signal_problem(
                task_signal_name(tasks["publish"].id, RUN_SKIPPED_STATUS)
            )
            == ""
        )
    finally:
        store.close()


def test_a_skipped_step_is_not_something_to_be_told_about(tmp_path):
    """One problem, not a dozen: the failure above is already asking."""
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["collect"].id)

        async def scenario():
            service = make_service(store, failing={"analyze"})
            for hop in range(6):
                await service.run_once(now=NOW + timedelta(seconds=30 * hop))

        asyncio.run(scenario())

        from agent.scheduler import run_needs_attention

        skipped = store.list_runs(tasks["publish"].id)[0]
        failed = store.list_runs(tasks["analyze"].id)[0]
        assert run_needs_attention(skipped) is False
        assert run_needs_attention(failed) is True
        # And the same rule as the one SQL query the interface uses, so the two
        # cannot drift into disagreeing about what the badge counts.
        counts = store.unacknowledged_attention_counts()
        assert counts == {tasks["analyze"].id: 1}
    finally:
        store.close()


def test_a_skip_does_not_claim_the_step_has_ever_run(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["collect"].id)

        async def scenario():
            service = make_service(store, failing={"collect"})
            for hop in range(6):
                await service.run_once(now=NOW + timedelta(seconds=30 * hop))

        asyncio.run(scenario())

        publish = store.get_task(tasks["publish"].id)
        assert publish.last_run_at is None
        assert publish.last_success_at is None
    finally:
        store.close()


def test_a_step_with_work_already_queued_is_not_skipped(tmp_path):
    """An earlier round's queued run is work somebody asked for.

    The failure arrives in a *later* round than the run still sitting in the
    queue.  Cancelling that run to satisfy the skip would throw away work that
    was legitimately asked for, so the step is left alone and its own outcome
    is allowed to speak -- and if it fails, the skip happens then.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)

        # One round that got as far as queueing publish and has not run it yet.
        store.emit_signal(
            task_signal_name(tasks["analyze"].id, RUN_SUCCESS_STATUS), source="manual"
        )
        store.deliver_signals(now=NOW)
        assert [item.status for item in store.list_runs(tasks["publish"].id)] == [
            "queued"
        ]

        # A running run for analyze, written directly so that claiming it does
        # not also claim publish's queued one.
        store._conn.execute(
            """
            INSERT INTO scheduled_task_runs (
                id, task_id, scheduled_for, started_at, status, summary, error,
                output_path, delivery_status, config_snapshot_json,
                trigger_source, attempt, missed_count, created_at, updated_at
            ) VALUES ('analyze-run', ?, ?, ?, 'running', '', '', '', '', '{}',
                      'schedule', 1, 0, ?, ?)
            """,
            (
                tasks["analyze"].id,
                NOW.isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        store._conn.execute(
            """
            UPDATE scheduled_tasks SET active_run_id = 'analyze-run', lease_until = ?
            WHERE id = ?
            """,
            ((NOW + timedelta(minutes=5)).isoformat(), tasks["analyze"].id),
        )
        store._conn.commit()

        assert store.complete_run(
            tasks["analyze"].id,
            "analyze-run",
            finished_at=NOW,
            status="failed",
            summary="boom",
        )

        # The queued run survives, and no skipped record was written.
        assert [item.status for item in store.list_runs(tasks["publish"].id)] == [
            "queued"
        ]
    finally:
        store.close()


def test_a_skipped_step_can_still_run_next_round(tmp_path):
    """Being skipped is a verdict on one round, not on the step."""
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["collect"].id)

        async def scenario():
            service = make_service(store, failing={"analyze"})
            for hop in range(6):
                await service.run_once(now=NOW + timedelta(seconds=30 * hop))
            # The next round goes well, started by hand so the entry step does
            # not have to be re-armed.
            service = make_service(store)
            store.emit_signal(
                task_signal_name(tasks["collect"].id, RUN_SUCCESS_STATUS),
                source="manual",
            )
            for hop in range(6, 14):
                await service.run_once(now=NOW + timedelta(seconds=30 * hop))

        asyncio.run(scenario())

        assert [item.status for item in store.list_runs(tasks["analyze"].id)] == [
            "failed",
            "succeeded",
        ]
        assert [item.status for item in store.list_runs(tasks["publish"].id)] == [
            "skipped",
            "succeeded",
        ]
    finally:
        store.close()


def test_a_step_that_succeeded_skips_nothing(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["collect"].id)

        async def scenario():
            service = make_service(store)
            for hop in range(6):
                await service.run_once(now=NOW + timedelta(seconds=30 * hop))

        asyncio.run(scenario())

        assert [item.status for item in store.list_runs(tasks["publish"].id)] == [
            "succeeded"
        ]
    finally:
        store.close()


def test_a_standalone_task_failing_skips_nothing(tmp_path):
    """Nothing is blocked, because nothing was waiting on it."""
    store = make_store(tmp_path)
    try:
        task = store.create_task(
            NewScheduledTask(
                name="lonely",
                kind="agent_prompt",
                trigger=clock(),
                payload={"prompt": "do the thing"},
                delivery_mode="standalone",
                delivery_target=DeliveryTarget.standalone(),
            )
        )
        make_due(store, task.id)

        async def scenario():
            service = make_service(store, failing={"lonely"})
            for hop in range(4):
                await service.run_once(now=NOW + timedelta(seconds=30 * hop))

        asyncio.run(scenario())

        assert [item.status for item in store.list_runs(task.id)] == ["failed"]
        assert len(store.list_tasks()) == 1
    finally:
        store.close()


# ── 6. A step is still an ordinary task ────────────────────────────────────


def test_a_standalone_task_is_not_mistaken_for_a_step(tmp_path):
    """Two identical definitions are two different things when one is a step."""
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(
            Workflow(name="job", steps=[step("collect", trigger=clock())])
        )
        step_task = store.step_tasks(workflow.id)["collect"]

        spec = NewScheduledTask(
            name=step_task.name,
            kind=step_task.kind,
            trigger=step_task.trigger,
            payload=step_task.payload,
            delivery_mode=step_task.delivery_mode,
            delivery_target=DeliveryTarget.standalone(),
            workspace_root=step_task.workspace_root,
        )
        assert store.find_matching_task(spec) is None

        plain = store.create_task(spec)
        assert plain.workflow_id == ""
        assert plain.step_key == ""
        assert plain.id != step_task.id
    finally:
        store.close()


def test_a_task_that_is_not_a_step_reports_no_placement(tmp_path):
    store = make_store(tmp_path)
    try:
        task = store.create_task(
            NewScheduledTask(
                name="standalone",
                kind="message",
                trigger=clock(),
                payload={"message_text": "hello"},
                delivery_mode="standalone",
                delivery_target=DeliveryTarget.standalone(),
            )
        )
        assert task.workflow_id == ""
        assert task.step_key == ""
        assert store.list_workflows() == []
    finally:
        store.close()


def test_a_renamed_step_keeps_its_edges(tmp_path):
    """The edge is an id, so renaming a step cannot silently sever it."""
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)

        renamed = store.get_workflow(workflow.id)
        renamed.steps[0] = step("collect", name="collect the numbers", trigger=clock())
        store.update_workflow(workflow.id, renamed)

        after = store.step_tasks(workflow.id)
        assert after["collect"].id == tasks["collect"].id
        assert after["collect"].name == "collect the numbers"
        assert signal_names(after["analyze"].trigger) == [
            task_signal_name(after["collect"].id, RUN_SUCCESS_STATUS)
        ]
        assert parse_task_signal(signal_names(after["analyze"].trigger)[0]) == (
            after["collect"].id,
            "succeeded",
        )
    finally:
        store.close()


# ── 7. What a step is told about the steps above it ────────────────────────
#
# A step runs because its upstreams succeeded, and for a long time that was
# all it knew: a signal said "something finished", and the step's own prompt
# was the only other input.  So "send the previous step's result" had no
# answer, and the workaround was for the upstream to write a file somewhere
# both steps happened to look.
#
# Three things were missing, and each has its own test below:
#
#   1. the emission carried no address, so a step could not say where its
#      output went even to a reader that wanted to look;
#   2. a join kept only the payload of whichever arm finished last, so a step
#      with two upstreams was told about one of them and nothing said which;
#   3. nothing rendered any of it into the run, so the field was write-only.


def make_handoff_service(store: SchedulerStore, output_root: Path) -> SchedulerService:
    """A service that keeps each run's output, the way standalone delivery does.

    The real ``deliver_standalone`` writes ``<root>/<task>/<run>.md`` and
    records the path on the run; a test that wants to read an upstream's output
    has to produce that file, or it is testing a pointer to nothing.
    """

    async def executor(task, run):
        return ExecutionResult(
            summary=f"ran {task.name}", text_output=f"out {task.name}"
        )

    async def unused(*args, **kwargs):
        raise AssertionError("system executor should not be called")

    async def delivery(task, run, result):
        directory = output_root / task.id
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{run.id}.md"
        path.write_text(result.text_output, encoding="utf-8")
        return DeliveryResult(status="stored", output_path=str(path))

    return SchedulerService(
        store=store,
        agent_executor=executor,
        system_executor=unused,
        delivery=delivery,
        poll_seconds=30,
        lease_seconds=300,
    )


def run_rounds(service: SchedulerService, hops: int) -> None:
    async def scenario():
        for hop in range(hops):
            await service.run_once(now=NOW + timedelta(seconds=30 * hop))

    asyncio.run(scenario())


def test_a_run_signal_says_where_its_output_went(tmp_path):
    """A step's result is a file with a path, and the path has to travel.

    Without it the downstream step knows a step succeeded and cannot say where
    its work is -- which is the difference between a notification and a
    handoff.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["collect"].id)
        run_rounds(make_handoff_service(store, tmp_path / "output"), 4)

        payload = store.list_runs(tasks["analyze"].id)[0].config_snapshot["signal"][
            "payload"
        ]
        assert payload["step_key"] == "collect"
        assert payload["workflow_id"] == workflow.id
        assert Path(payload["output_path"]).is_file()
        assert Path(payload["output_path"]).read_text(encoding="utf-8") == "out collect"
    finally:
        store.close()


def test_a_linear_step_is_told_about_the_one_step_above_it(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["collect"].id)
        run_rounds(make_handoff_service(store, tmp_path / "output"), 4)

        signals = store.list_runs(tasks["analyze"].id)[0].config_snapshot["signals"]
        assert [item["payload"]["step_key"] for item in signals] == ["collect"]
    finally:
        store.close()


def test_a_join_hands_the_step_below_every_upstream_not_just_the_last(tmp_path):
    """The bug this section exists for.

    A join fires when the last arm reports, and the run it queues used to be
    built from that one emission -- so the other arm's result was recorded as
    an arrival and then dropped, and the step below could not tell that it had
    two upstreams at all.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(fork_workflow())
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["start"].id)
        run_rounds(make_handoff_service(store, tmp_path / "output"), 6)

        snapshot = store.list_runs(tasks["join"].id)[0].config_snapshot
        signals = snapshot["signals"]
        # Both arms, not just the one that finished last.  Order is the store's
        # (by signal name, which embeds a task id), so this compares as a set;
        # the order a reader sees is settled by the prompt block, below.
        assert sorted(item["payload"]["step_key"] for item in signals) == [
            "left",
            "right",
        ]
        assert all(Path(item["payload"]["output_path"]).is_file() for item in signals)
        # ``signal`` still means "the emission that woke this run", which is
        # one of the two -- the two keys answer different questions.
        assert snapshot["signal"]["payload"]["step_key"] in {"left", "right"}
    finally:
        store.close()


def test_each_round_of_a_join_carries_only_that_rounds_arms(tmp_path):
    """Arrivals are spent with the round, so a later run is not told about an
    earlier one -- a payload that outlives its round describes work that has
    already been acted on."""
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(fork_workflow())
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["start"].id)
        service = make_handoff_service(store, tmp_path / "output")
        run_rounds(service, 6)
        store.emit_signal(
            task_signal_name(tasks["start"].id, RUN_SUCCESS_STATUS), source="manual"
        )
        run_rounds(service, 6)

        rounds = store.list_runs(tasks["join"].id)
        assert len(rounds) == 2
        for run in rounds:
            signals = run.config_snapshot["signals"]
            assert sorted(item["payload"]["step_key"] for item in signals) == [
                "left",
                "right",
            ]
    finally:
        store.close()


def test_a_join_row_from_before_payloads_were_kept_reads_as_no_arrivals(tmp_path):
    """An existing database has join rows with names and no payloads -- the
    migration fills the new column with an empty object.  Those rows have to
    keep working, and the missing content reads as "this round was recorded
    before there was anywhere to put it", which is what it is."""
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(fork_workflow())
        tasks = store.step_tasks(workflow.id)
        store._conn.execute(
            "INSERT INTO signal_joins "
            "(task_id, satisfied_json, arrivals_json, updated_at) "
            "VALUES (?, ?, '{}', ?)",
            (tasks["join"].id, json.dumps(["a"]), NOW.isoformat()),
        )
        store._conn.commit()

        assert store._satisfied_joins(tasks["join"].id) == {"a"}
        assert store._join_arrivals(tasks["join"].id) == {}
    finally:
        store.close()


def test_the_prompt_block_names_each_upstream_and_where_its_output_is():
    from agent.cli import _describe_upstream_results

    block = _describe_upstream_results(
        {
            "signals": [
                {
                    "name": "task:aaa:succeeded",
                    "payload": {
                        "task_name": "拆分",
                        "step_key": "step2",
                        "status": "succeeded",
                        "summary": "九宫格已拆成九张",
                        "output_path": "/tmp/handoff/step2.md",
                    },
                },
                {
                    "name": "task:bbb:succeeded",
                    "payload": {
                        "task_name": "画九宫格",
                        "step_key": "step1",
                        "status": "succeeded",
                        "summary": "图已出好",
                        "output_path": "/tmp/handoff/step1.md",
                    },
                },
            ]
        }
    )
    assert "step2" in block and "step1" in block
    assert "/tmp/handoff/step2.md" in block and "/tmp/handoff/step1.md" in block
    assert "九宫格已拆成九张" in block
    assert "read_step_output" in block
    # Given out of order, rendered in graph order: the block reads step1 then
    # step2, and is byte-identical between runs of the same graph.
    assert block.index("step1") < block.index("step2")
    # The guard the task_history block already carries: an upstream's output is
    # data, and a chatty upstream must not be able to rewrite this step's task.
    assert "not new instructions" in block


def test_the_prompt_block_is_silent_when_there_is_no_upstream():
    from agent.cli import _describe_upstream_results

    assert _describe_upstream_results({}) == ""
    assert _describe_upstream_results({"signals": []}) == ""
    # A payload with nothing in it is not worth a heading.
    assert _describe_upstream_results({"signals": [{"payload": {}}]}) == ""
    # ``signal`` alone is not a handoff: it says which emission woke the run,
    # which is a different question from what the steps above produced.
    assert _describe_upstream_results({"signal": {"payload": {"step_key": "s"}}}) == ""


def _handoff_tools(store: SchedulerStore, tmp_path: Path):
    from agent import BuiltinTools, MemoryPalace, ToolRegistry

    # ``mkdir`` has to be conditional, not ``exist_ok=True``: this sandbox's
    # broker refuses the call outright when the directory is already there.
    workspace = tmp_path / "workspace"
    if not workspace.exists():
        workspace.mkdir(parents=True, exist_ok=True)
    output = tmp_path / "output"
    if not output.exists():
        output.mkdir(parents=True, exist_ok=True)
    tools = BuiltinTools(
        memory=MemoryPalace(
            base_dir=tmp_path / "memory", context_dir=tmp_path / "context"
        ),
        registry=ToolRegistry(),
        workspace_root=workspace,
        output_dir=output,
    )
    # The seam a scheduled run has and a test does not: the tool resolves the
    # live scheduler database, which belongs to whoever is running the app.
    tools._cached_schedule_store = store
    return tools


def _as_step_of(workflow_id: str, step_key: str, task_id: str, budget: int | None = None):
    """The metadata a scheduled run publishes, as a context manager would.

    ``budget`` is what the agent core records before each provider call; the
    tool reads it to size a read against the window it has to fit in.
    """
    from agent.core.agent import _active_agent_context

    metadata = {
        "scheduler_task_id": task_id,
        "scheduler_step_key": step_key,
        "scheduler_workflow_id": workflow_id,
    }
    if budget is not None:
        metadata["_last_input_token_budget"] = budget
    return _active_agent_context.set(SimpleNamespace(metadata=metadata))


def test_read_step_output_returns_what_the_upstream_step_produced(tmp_path):
    from agent.core.agent import _active_agent_context

    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["collect"].id)
        run_rounds(make_handoff_service(store, tmp_path / "output"), 4)

        tools = _handoff_tools(store, tmp_path)
        token = _as_step_of(workflow.id, "analyze", tasks["analyze"].id)
        try:
            result = tools._read_step_output("collect")
        finally:
            _active_agent_context.reset(token)

        assert result["ok"] is True
        assert result["step"] == "collect"
        assert result["text"] == "out collect"
        assert result["truncated"] is False
        assert Path(result["output_path"]).is_file()
    finally:
        store.close()


def test_read_step_output_refuses_a_step_of_another_workflow(tmp_path):
    """The workflow comes from the run being served, not from an argument, so
    naming a step of a different graph cannot reach it."""
    from agent.core.agent import _active_agent_context

    store = make_store(tmp_path)
    try:
        mine = store.create_workflow(linear_workflow())
        store.create_workflow(fork_workflow())
        tasks = store.step_tasks(mine.id)

        tools = _handoff_tools(store, tmp_path)
        token = _as_step_of(mine.id, "analyze", tasks["analyze"].id)
        try:
            result = tools._read_step_output("join")
        finally:
            _active_agent_context.reset(token)

        assert result["ok"] is False
        assert "join" in result["error"]
        assert "collect" in result["error"]
    finally:
        store.close()


def test_read_step_output_refuses_to_read_the_current_step(tmp_path):
    """Reading yourself returns the previous round's text as though it were the
    upstream's, which is the kind of answer that looks right and is not."""
    from agent.core.agent import _active_agent_context

    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)

        tools = _handoff_tools(store, tmp_path)
        token = _as_step_of(workflow.id, "analyze", tasks["analyze"].id)
        try:
            result = tools._read_step_output("analyze")
        finally:
            _active_agent_context.reset(token)

        assert result["ok"] is False
        assert "当前这一步" in result["error"]
    finally:
        store.close()


def test_read_step_output_refuses_outside_a_workflow(tmp_path):
    """An ordinary scheduled task has no graph to resolve a step key against,
    and saying so beats returning something that is not what was asked for."""
    from agent.core.agent import _active_agent_context

    store = make_store(tmp_path)
    try:
        tools = _handoff_tools(store, tmp_path)
        token = _as_step_of("", "", "some-standalone-task")
        try:
            result = tools._read_step_output("step2")
        finally:
            _active_agent_context.reset(token)

        assert result["ok"] is False
        assert "不属于任何流程" in result["error"]
    finally:
        store.close()


def test_read_step_output_says_so_when_there_is_nothing_to_read(tmp_path):
    """A step that has not run successfully has no output, and the difference
    between "no output yet" and "empty output" is worth keeping."""
    from agent.core.agent import _active_agent_context

    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)

        tools = _handoff_tools(store, tmp_path)
        token = _as_step_of(workflow.id, "analyze", tasks["analyze"].id)
        try:
            result = tools._read_step_output("collect")
        finally:
            _active_agent_context.reset(token)

        assert result["ok"] is False
        assert "还没有成功过" in result["error"]
    finally:
        store.close()


def test_read_step_output_refuses_a_step_that_is_not_upstream(tmp_path):
    """A step beside or below this one has not run for this round, so reading
    it hands back an earlier round's text as if it were this round's work --
    the same failure as reading your own output, one edge further out."""
    from agent.core.agent import _active_agent_context

    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        tools = _handoff_tools(store, tmp_path)

        token = _as_step_of(workflow.id, "analyze", tasks["analyze"].id)
        try:
            result = tools._read_step_output("publish")
        finally:
            _active_agent_context.reset(token)

        assert result["ok"] is False
        assert "不是当前这一步的上游" in result["error"]
        # The refusal names what it would have accepted, so the next attempt
        # does not have to guess.
        assert "collect" in result["error"]
    finally:
        store.close()


def test_read_step_output_allows_a_step_two_edges_up(tmp_path):
    """``publish`` stands on ``analyze`` and, through it, on ``collect``."""
    from agent.core.agent import _active_agent_context

    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        directory = tmp_path / "output" / tasks["collect"].id
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
        path = directory / "manual.md"
        path.write_text("the upstream report", encoding="utf-8")
        store._conn.execute(
            "INSERT INTO scheduled_task_runs ("
            " id, task_id, scheduled_for, started_at, finished_at, status,"
            " summary, error, output_path, delivery_status, config_snapshot_json,"
            " trigger_source, attempt, missed_count, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, 'succeeded', 'done', '', ?, '', '{}',"
            " 'manual', 1, 0, ?, ?)",
            (
                "run-collect",
                tasks["collect"].id,
                NOW.isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
                str(path),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        store._conn.commit()

        tools = _handoff_tools(store, tmp_path)
        token = _as_step_of(workflow.id, "publish", tasks["publish"].id)
        try:
            result = tools._read_step_output("collect")
        finally:
            _active_agent_context.reset(token)

        assert result["ok"] is True
        assert result["text"] == "the upstream report"
    finally:
        store.close()


def test_read_step_output_truncates_and_says_that_it_did(tmp_path):
    """A step that believes it read the whole report and got two thirds of it
    will act on the two thirds."""
    from agent.core.agent import _active_agent_context

    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        directory = tmp_path / "output" / tasks["collect"].id
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
        path = directory / "manual.md"
        path.write_text("x" * 500, encoding="utf-8")
        store._conn.execute(
            "INSERT INTO scheduled_task_runs ("
            " id, task_id, scheduled_for, started_at, finished_at, status,"
            " summary, error, output_path, delivery_status, config_snapshot_json,"
            " trigger_source, attempt, missed_count, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, 'succeeded', 'done', '', ?, '', '{}',"
            " 'manual', 1, 0, ?, ?)",
            (
                "run-manual",
                tasks["collect"].id,
                NOW.isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
                str(path),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        store._conn.commit()

        tools = _handoff_tools(store, tmp_path)
        token = _as_step_of(workflow.id, "analyze", tasks["analyze"].id)
        try:
            result = tools._read_step_output("collect", max_chars=100)
        finally:
            _active_agent_context.reset(token)

        assert result["ok"] is True
        assert len(result["text"]) == 100
        assert result["truncated"] is True
        assert result["total_chars"] == 500
        assert "output_path" in result["note"]
    finally:
        store.close()


# ── 8. A step is told about its upstreams however it was started ──────────
#
# The handoff used to be a by-product of the emission that queued the run,
# which made it a property of *how the run started* rather than of *what the
# step needs*.  A step started by hand, or retried with the latest
# configuration, produced a run that looked exactly like one that had been
# told everything and had been told nothing.  The trigger already names the
# upstreams, so the answer was in hand and simply unread.


def test_a_step_started_by_hand_is_told_about_its_upstreams(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["collect"].id)
        run_rounds(make_handoff_service(store, tmp_path / "output"), 4)

        claimed = store.claim_task_now(
            tasks["analyze"].id, now=NOW + timedelta(hours=1)
        )

        assert claimed is not None
        assert claimed.run.trigger_source == "manual"
        signals = claimed.run.config_snapshot["signals"]
        assert [item["payload"]["step_key"] for item in signals] == ["collect"]
        assert Path(signals[0]["payload"]["output_path"]).is_file()
    finally:
        store.close()


def test_a_step_retried_with_the_latest_config_is_told_about_its_upstreams(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["collect"].id)
        run_rounds(make_handoff_service(store, tmp_path / "output"), 4)

        source = store.list_runs(tasks["analyze"].id)[-1]
        retried = store.claim_retry(
            tasks["analyze"].id,
            source.id,
            use_latest=True,
            now=NOW + timedelta(hours=1),
        )

        assert retried is not None
        assert retried.run.trigger_source == "retry_latest"
        signals = retried.run.config_snapshot["signals"]
        assert [item["payload"]["step_key"] for item in signals] == ["collect"]
    finally:
        store.close()


def test_a_join_started_by_hand_still_carries_every_arm(tmp_path):
    """Fan-in is a property of the graph, so it survives the run being started
    some other way -- which is the whole reason it moved off the emission."""
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(fork_workflow())
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["start"].id)
        run_rounds(make_handoff_service(store, tmp_path / "output"), 6)

        claimed = store.claim_task_now(
            tasks["join"].id, now=NOW + timedelta(hours=1)
        )

        assert claimed is not None
        signals = claimed.run.config_snapshot["signals"]
        assert sorted(item["payload"]["step_key"] for item in signals) == [
            "left",
            "right",
        ]
    finally:
        store.close()


def test_a_run_nobody_woke_does_not_claim_it_was_woken(tmp_path):
    """``signal`` means "the emission that woke this run", and cascade depth is
    inherited from it.  A hand-started run has no parent; saying it had one
    would put it one hop deeper into a cascade that does not exist."""
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["collect"].id)
        run_rounds(make_handoff_service(store, tmp_path / "output"), 4)

        woken = store.list_runs(tasks["analyze"].id)[-1]
        assert woken.trigger_source.startswith("signal:")
        assert "signal" in woken.config_snapshot

        claimed = store.claim_task_now(
            tasks["analyze"].id, now=NOW + timedelta(hours=1)
        )
        assert "signals" in claimed.run.config_snapshot
        assert "signal" not in claimed.run.config_snapshot
    finally:
        store.close()


def test_an_entry_step_started_by_hand_is_told_nothing_about_upstreams(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)

        claimed = store.claim_task_now(tasks["collect"].id, now=NOW)

        assert claimed is not None
        assert "signals" not in claimed.run.config_snapshot
    finally:
        store.close()


def test_retrying_from_the_run_snapshot_does_not_re_derive_the_upstreams(tmp_path):
    """``retry_snapshot`` replays the run as it was, so it is told what it was
    told.  The claim only fills ``signals`` when nothing else has -- asserted
    by planting a list that re-deriving would have overwritten."""
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        store._conn.execute(
            "INSERT INTO scheduled_task_runs ("
            " id, task_id, scheduled_for, started_at, finished_at, status,"
            " summary, error, output_path, delivery_status, config_snapshot_json,"
            " trigger_source, attempt, missed_count, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, 'succeeded', 'done', '', '', '', ?,"
            " 'manual', 1, 0, ?, ?)",
            (
                "run-ghost",
                tasks["analyze"].id,
                NOW.isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
                json.dumps(
                    {
                        "signals": [
                            {"name": "task:ghost:succeeded", "payload": {"step_key": "ghost"}}
                        ]
                    }
                ),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
        store._conn.commit()

        retried = store.claim_retry(
            tasks["analyze"].id, "run-ghost", now=NOW + timedelta(hours=1)
        )

        assert retried is not None
        assert retried.run.trigger_source == "retry_snapshot"
        assert [
            item["payload"]["step_key"]
            for item in retried.run.config_snapshot["signals"]
        ] == ["ghost"]
    finally:
        store.close()


# ── 9. What a run produced is written down whoever it was also sent to ────


def _channel_service(store: SchedulerStore, output_root: Path, sent: list):
    """A service whose runs deliver to a channel, through the real delivery.

    ``deliver_channel`` is the only part replaced: the point under test is what
    the delivery layer does *around* the send, so the send itself is stubbed
    and everything else is the production code path.
    """
    from agent.scheduler.delivery import SchedulerDelivery

    delivery = SchedulerDelivery(cfg={}, output_root=output_root)

    async def fake_channel(*, target, text, output_dir=None):
        sent.append(text)
        return "delivered"

    delivery.deliver_channel = fake_channel

    async def executor(task, run):
        return ExecutionResult(
            summary=f"ran {task.name}", text_output=f"out {task.name}"
        )

    async def unused(*args, **kwargs):
        raise AssertionError("system executor should not be called")

    return SchedulerService(
        store=store,
        agent_executor=executor,
        system_executor=unused,
        delivery=delivery,
        poll_seconds=30,
        lease_seconds=300,
    )


def test_a_run_that_only_notified_a_channel_is_still_written_down(tmp_path):
    """What a run produced is a fact about the run; sending it to a chat is an
    extra action on top of that fact.  Only the standalone mode used to write
    it down, so a step that notified a channel left nothing behind -- and
    nothing behind is unrecoverable, because the row keeps a 120-character
    summary and the text itself is gone."""
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        store._conn.execute(
            "UPDATE scheduled_tasks SET delivery_mode = 'channel' WHERE id = ?",
            (tasks["analyze"].id,),
        )
        store._conn.commit()

        sent: list = []
        make_due(store, tasks["collect"].id)
        run_rounds(_channel_service(store, tmp_path / "output", sent), 4)

        assert sent, "the middle step should have reached the channel"
        run = [item for item in store.list_runs(tasks["analyze"].id) if item.status == "succeeded"][-1]
        assert run.delivery_status == "delivered"
        assert run.output_path
        assert Path(run.output_path).read_text(encoding="utf-8") == "out analyze"
    finally:
        store.close()


def test_the_step_below_can_read_a_step_that_only_notified_a_channel(tmp_path):
    """The payoff: a notifying step used to be a dead end for the graph."""
    from agent.core.agent import _active_agent_context

    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        store._conn.execute(
            "UPDATE scheduled_tasks SET delivery_mode = 'channel' WHERE id = ?",
            (tasks["analyze"].id,),
        )
        store._conn.commit()

        sent: list = []
        make_due(store, tasks["collect"].id)
        run_rounds(_channel_service(store, tmp_path / "output", sent), 4)

        tools = _handoff_tools(store, tmp_path)
        token = _as_step_of(workflow.id, "publish", tasks["publish"].id)
        try:
            result = tools._read_step_output("analyze")
        finally:
            _active_agent_context.reset(token)

        assert result["ok"] is True
        assert result["text"] == "out analyze"
    finally:
        store.close()


def test_a_failed_notification_still_leaves_the_artifact(tmp_path, monkeypatch):
    """The write happens before the send, so a delivery that fails does not
    also destroy the record of what the run produced."""
    import asyncio as _asyncio

    from agent.scheduler.delivery import SchedulerDelivery

    delivery = SchedulerDelivery(cfg={}, output_root=tmp_path / "output")

    async def fail(*args, **kwargs):
        raise RuntimeError("Feishu unavailable")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(delivery, "deliver_channel", fail)
    monkeypatch.setattr(_asyncio, "sleep", no_sleep)

    result = _asyncio.run(
        delivery.deliver(
            task_id="task",
            run_id="run",
            delivery_mode="channel",
            target=DeliveryTarget.channel(
                target_type="feishu_chat", chat_id="oc_test", chat_type="group"
            ),
            text="the report",
            max_retries=1,
        )
    )

    assert result.status == "failed"
    assert result.output_path
    assert Path(result.output_path).read_text(encoding="utf-8") == "the report"


# ── 10. What the block says about a result it is only pointing at ─────────


def test_the_prompt_block_says_how_big_the_upstream_output_is():
    """A pointer with no size is a pointer you have to follow in order to
    evaluate, which is the cost the pointer existed to avoid."""
    from agent.cli import _describe_upstream_results

    block = _describe_upstream_results(
        {
            "signals": [
                {
                    "name": "task:aaa:succeeded",
                    "payload": {
                        "task_name": "拆分",
                        "step_key": "step2",
                        "status": "succeeded",
                        "summary": "九宫格已拆成九张",
                        "output_path": "/tmp/handoff/step2.md",
                        "output_bytes": 2724,
                    },
                }
            ]
        }
    )
    assert "/tmp/handoff/step2.md (2724 bytes)" in block


def test_the_prompt_block_calls_the_summary_a_preview():
    """An unlabelled 27-character summary next to a 1372-character report
    reads like the whole result, and a step that believes it has the result
    does not go and fetch it."""
    from agent.cli import _describe_upstream_results

    block = _describe_upstream_results(
        {
            "signals": [
                {
                    "name": "task:aaa:succeeded",
                    "payload": {
                        "task_name": "拆分",
                        "step_key": "step2",
                        "status": "succeeded",
                        "summary": "九宫格已拆成九张",
                    },
                }
            ]
        }
    )
    assert "first line only" in block
    assert "summary: 九宫格已拆成九张" not in block


def test_the_prompt_block_does_not_invent_a_size_it_was_not_given():
    from agent.cli import _describe_upstream_results

    block = _describe_upstream_results(
        {
            "signals": [
                {
                    "name": "task:aaa:succeeded",
                    "payload": {
                        "task_name": "拆分",
                        "step_key": "step2",
                        "output_path": "/tmp/handoff/step2.md",
                    },
                }
            ]
        }
    )
    assert "/tmp/handoff/step2.md" in block
    assert "bytes" not in block


def test_an_emission_says_how_big_the_output_was(tmp_path):
    """The size travels with the address from the moment the run finishes."""
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["collect"].id)
        run_rounds(make_handoff_service(store, tmp_path / "output"), 4)

        emissions = store.list_emissions(
            name=task_signal_name(tasks["collect"].id, RUN_SUCCESS_STATUS)
        )
        assert emissions[0].payload["output_bytes"] == len("out collect")
    finally:
        store.close()


# ── 11. A read is sized against the window it has to fit in ──────────────


def _plant_output(store: SchedulerStore, task, tmp_path: Path, text: str) -> str:
    """Give a step a successful run with an output file, the way delivery does."""
    directory = tmp_path / "output" / task.id
    if not directory.exists():
        directory.mkdir(parents=True, exist_ok=True)
    path = directory / "planted.md"
    path.write_text(text, encoding="utf-8")
    store._conn.execute(
        "INSERT INTO scheduled_task_runs ("
        " id, task_id, scheduled_for, started_at, finished_at, status,"
        " summary, error, output_path, delivery_status, config_snapshot_json,"
        " trigger_source, attempt, missed_count, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, 'succeeded', 'done', '', ?, '', '{}',"
        " 'manual', 1, 0, ?, ?)",
        (
            "run-planted",
            task.id,
            NOW.isoformat(),
            NOW.isoformat(),
            NOW.isoformat(),
            str(path),
            NOW.isoformat(),
            NOW.isoformat(),
        ),
    )
    store._conn.commit()
    return str(path)


def test_read_step_output_clamps_to_a_share_of_the_input_budget(tmp_path):
    """CJK runs about one token per character, so the flat 40000-character
    ceiling was three quarters of a 53841-token budget in a single call -- and
    the run then died of the limit it had just walked into, losing everything
    it had done and reporting a provider error that names no tool."""
    from agent.core.agent import _active_agent_context
    from agent.tools.builtin_tools import READ_STEP_OUTPUT_MAX_CHARS

    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        _plant_output(store, tasks["collect"], tmp_path, "字" * 40000)

        tools = _handoff_tools(store, tmp_path)
        token = _as_step_of(workflow.id, "analyze", tasks["analyze"].id, budget=4000)
        try:
            result = tools._read_step_output(
                "collect", max_chars=READ_STEP_OUTPUT_MAX_CHARS
            )
        finally:
            _active_agent_context.reset(token)

        # A quarter of 4000 tokens, and one Chinese character is one token.
        assert len(result["text"]) == 1000
        assert result["truncated"] is True
        # Told why, not just that: a caller told "less than you asked for"
        # with no reason simply asks again.
        assert "4000 tokens" in result["note"]
        assert "1000" in result["note"]
        assert "output_path" in result["note"]
    finally:
        store.close()


def test_read_step_output_keeps_a_comfortable_read_when_the_budget_allows(tmp_path):
    from agent.core.agent import _active_agent_context
    from agent.tools.builtin_tools import READ_STEP_OUTPUT_DEFAULT_CHARS

    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        _plant_output(store, tasks["collect"], tmp_path, "x" * 50000)

        tools = _handoff_tools(store, tmp_path)
        token = _as_step_of(
            workflow.id, "analyze", tasks["analyze"].id, budget=200000
        )
        try:
            result = tools._read_step_output("collect")
        finally:
            _active_agent_context.reset(token)

        assert len(result["text"]) == READ_STEP_OUTPUT_DEFAULT_CHARS
        # The default bound, not the budget: the note must not blame the
        # window for a limit the caller's own default set.
        assert "预算" not in result["note"]
    finally:
        store.close()


def test_read_step_output_uses_the_static_ceiling_when_no_budget_is_known(tmp_path):
    """A tool can be called outside a run -- from a test, or straight off the
    registry -- and then there is no window to size against."""
    from agent.core.agent import _active_agent_context
    from agent.tools.builtin_tools import READ_STEP_OUTPUT_MAX_CHARS

    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        _plant_output(store, tasks["collect"], tmp_path, "x" * 50000)

        tools = _handoff_tools(store, tmp_path)
        token = _as_step_of(workflow.id, "analyze", tasks["analyze"].id)
        try:
            result = tools._read_step_output(
                "collect", max_chars=READ_STEP_OUTPUT_MAX_CHARS
            )
        finally:
            _active_agent_context.reset(token)

        assert len(result["text"]) == READ_STEP_OUTPUT_MAX_CHARS
        assert "预算" not in result["note"]
    finally:
        store.close()


def test_read_step_output_asks_for_more_when_the_budget_is_tiny(tmp_path):
    """The default is only comfortable if the window can afford it.  A small
    budget has to clamp the default too, or the footgun just moves."""
    from agent.core.agent import _active_agent_context

    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        _plant_output(store, tasks["collect"], tmp_path, "字" * 5000)

        tools = _handoff_tools(store, tmp_path)
        token = _as_step_of(workflow.id, "analyze", tasks["analyze"].id, budget=400)
        try:
            result = tools._read_step_output("collect")
        finally:
            _active_agent_context.reset(token)

        assert len(result["text"]) == 100
        assert "400 tokens" in result["note"]
    finally:
        store.close()


def test_the_tool_clamps_against_the_budget_the_core_publishes(tmp_path):
    """The clamp is only real if the number it reads is the one the agent core
    records.  Driven through the real ``_prepare_provider_context`` rather than
    by writing the metadata by hand: a test that fakes the seam proves only
    that the seam was faked."""
    import agent as agent_module
    from agent.core.agent import AgentContext, BaseAgent, _active_agent_context

    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        _plant_output(store, tasks["collect"], tmp_path, "字" * 40000)

        agent = BaseAgent(
            object(),
            agent_module.ToolRegistry(),
            model="fake-model",
            api_format="openai",
            context_window=20000,
            max_tokens=4096,
        )
        ctx = AgentContext(system_prompt="you are an assistant")
        # What the executor publishes on the same dict, plus what the core adds
        # before every provider call.
        ctx.metadata["scheduler_workflow_id"] = workflow.id
        ctx.metadata["scheduler_step_key"] = "analyze"
        agent._prepare_provider_context(
            ctx,
            [{"name": "read_file", "description": "read", "input_schema": {"type": "object"}}],
        )
        budget = ctx.metadata["_last_input_token_budget"]
        assert budget > 0

        handoff = _handoff_tools(store, tmp_path)
        token = _active_agent_context.set(ctx)
        try:
            result = handoff._read_step_output("collect", max_chars=40000)
        finally:
            _active_agent_context.reset(token)

        assert len(result["text"]) == max(1, budget // 4)
        assert f"{budget} tokens" in result["note"]
    finally:
        store.close()


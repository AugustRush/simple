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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from agent.scheduler import (
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

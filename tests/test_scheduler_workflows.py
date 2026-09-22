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
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent.scheduler import (
    DeliveryResult,
    DeliveryTarget,
    ExecutionResult,
    MAX_RETRY_ATTEMPTS,
    MAX_RETRY_BACKOFF_SECONDS,
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


def _remove_tree(path: Path) -> None:
    """Dispose of a scratch tree without caring what happened while we walked.

    The obvious loop -- ``child.unlink() if child.is_file() else child.rmdir()``
    -- reads "not a file" as "a directory", and re-tests it per child rather
    than against the walk that found it.  A file that disappears in between
    therefore falls into the ``rmdir`` branch and raises ``FileNotFoundError``
    on a path that is gone.  The palace leaves a SQLite ``-wal`` beside its
    database and removes it on a clean close, so the entry that vanishes is the
    normal case, not an exotic one; it turned the teardown of whichever test
    happened to lose the race into an ``ERROR`` that looked like a regression
    and reproduced roughly once in three full runs.

    ``rmtree`` also gets the nesting right, which the sort-based walk only
    approximated.
    """
    shutil.rmtree(path, ignore_errors=True)


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
    _remove_tree(path)


def test_scratch_cleanup_survives_a_path_that_is_already_gone(tmp_path):
    """Teardown must not raise over a path that is no longer there.

    The palace's SQLite ``-wal`` disappears on a clean close, so an entry the
    walk saw can be missing by the time the removal reaches it.  Reading "no
    longer a file" as "a directory" then called ``rmdir`` on a vanished path,
    which pytest reports as ``ERROR at teardown`` and reads as a regression.
    """
    target = tmp_path / "scratch"
    (target / "context").mkdir(parents=True)
    (target / "context" / "palace.db").write_text("db", encoding="utf-8")
    wal = target / "context" / "palace.db-wal"
    wal.write_text("wal", encoding="utf-8")

    # Gone before the removal gets to it -- the shape of the original failure.
    wal.unlink()
    _remove_tree(target)
    assert not target.exists()

    # Cleaning up a tree that is already gone is not an error either.
    _remove_tree(target)
    assert not target.exists()


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
    retry_policy: dict | None = None,
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
        **({"retry_policy": dict(retry_policy)} if retry_policy else {}),
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


def test_a_steps_retry_policy_reaches_the_task_behind_it(tmp_path):
    """A step that says "try twice" gets a task that tries twice.

    The retry path reads this off the run's config snapshot, which is built
    from the task row -- so a policy that stopped at the graph would leave the
    step running with the no-retry default, and one flake would skip every
    step below it.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(
            Workflow(
                name="flaky",
                steps=[
                    step("collect", trigger=clock()),
                    step(
                        "analyze",
                        depends_on=["collect"],
                        retry_policy={"max_attempts": 3, "backoff_seconds": 5},
                    ),
                ],
            )
        )
        tasks = store.step_tasks(workflow.id)

        assert tasks["analyze"].retry_policy == {
            "max_attempts": 3,
            "backoff_seconds": 5,
        }
        # A step that said nothing keeps the default, which is not to retry.
        assert tasks["collect"].retry_policy["max_attempts"] == 1
    finally:
        store.close()


def test_a_steps_retry_policy_survives_a_graph_edit(tmp_path):
    """Re-materializing rebuilds every task from its step, so the policy has to
    make the round trip through the stored graph as well as into the task."""
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(
            Workflow(
                name="flaky",
                steps=[
                    step("collect", trigger=clock()),
                    step(
                        "analyze",
                        depends_on=["collect"],
                        retry_policy={"max_attempts": 4, "backoff_seconds": 60},
                    ),
                ],
            )
        )
        stored = store.get_workflow(workflow.id)
        assert stored.step("analyze").retry_policy == {
            "max_attempts": 4,
            "backoff_seconds": 60,
        }

        store.materialize_workflow(workflow.id)
        again = store.step_tasks(workflow.id)["analyze"]
        assert again.retry_policy == {"max_attempts": 4, "backoff_seconds": 60}
    finally:
        store.close()


def test_a_step_from_a_graph_written_before_retries_does_not_retry(tmp_path):
    """An existing database has graphs with no such key.  Those steps have
    never retried, so reading them as "do not retry" is what keeps an old
    workflow behaving tomorrow the way it behaved yesterday."""
    raw = json.dumps(
        {
            "name": "old",
            "enabled": True,
            "steps": [
                {
                    "key": "collect",
                    "name": "collect",
                    "kind": "agent_prompt",
                    "payload": {"prompt": "do collect"},
                    "depends_on": [],
                    "trigger": clock().to_json(),
                }
            ],
        }
    )
    restored = Workflow.from_graph(raw)

    assert restored.step("collect").retry_policy == {
        "max_attempts": 1,
        "backoff_seconds": 30,
    }


def test_a_steps_retry_policy_is_clamped_rather_than_trusted(tmp_path):
    """A graph can be written straight to the store by an agent, with nobody
    left to report a bad number to.  An unbounded ``max_attempts`` is a step
    that never gives up, and the chain below it waits on a step still trying."""
    raw = json.dumps(
        {
            "name": "greedy",
            "enabled": True,
            "steps": [
                {
                    "key": "collect",
                    "name": "collect",
                    "kind": "agent_prompt",
                    "payload": {"prompt": "do collect"},
                    "depends_on": [],
                    "trigger": clock().to_json(),
                    "retry_policy": {
                        "max_attempts": 9999,
                        "backoff_seconds": 999999,
                    },
                },
                {
                    "key": "analyze",
                    "name": "analyze",
                    "kind": "agent_prompt",
                    "payload": {"prompt": "do analyze"},
                    "depends_on": ["collect"],
                    "retry_policy": "not an object",
                },
            ],
        }
    )
    restored = Workflow.from_graph(raw)

    assert restored.step("collect").retry_policy == {
        "max_attempts": MAX_RETRY_ATTEMPTS,
        "backoff_seconds": MAX_RETRY_BACKOFF_SECONDS,
    }
    # Unreadable is the no-retry default, not a crash: a graph that cannot be
    # read back is a workflow nobody can open or fix.
    assert restored.step("analyze").retry_policy == {
        "max_attempts": 1,
        "backoff_seconds": 30,
    }


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


def test_a_build_that_fails_partway_leaves_no_workflow(tmp_path, monkeypatch):
    """A graph is only visible once every task behind it exists.

    The failure is injected at the second step, so the first one's task has
    already been written when it happens.  Committing the graph before the
    build finished would leave a workflow that looks enabled, fires its head,
    and then stops -- with the steps below it subscribed to nothing.
    """
    store = make_store(tmp_path)
    try:
        real = store._step_task_spec
        calls: list[str] = []

        def explode(step_obj, trigger, **kwargs):
            calls.append(step_obj.key)
            if len(calls) == 2:
                raise RuntimeError("建任务时炸了")
            return real(step_obj, trigger, **kwargs)

        monkeypatch.setattr(store, "_step_task_spec", explode)
        with pytest.raises(RuntimeError):
            store.create_workflow(linear_workflow())
        assert len(calls) == 2, "第一步应该已经建过了，失败才有意义"
        assert store.list_workflows() == []
        assert store.list_tasks() == []
    finally:
        store.close()


def test_an_edit_that_fails_partway_leaves_the_old_graph_running(tmp_path, monkeypatch):
    """Half a refresh is a graph whose steps disagree with their tasks."""
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        before = {key: task.id for key, task in store.step_tasks(workflow.id).items()}
        stored_graph = store.get_workflow(workflow.id).to_graph()

        def explode(step_obj, trigger, **kwargs):
            raise RuntimeError("改任务时炸了")

        monkeypatch.setattr(store, "_step_task_spec", explode)
        with pytest.raises(RuntimeError):
            store.update_workflow(
                workflow.id,
                Workflow(
                    name="renamed",
                    steps=[
                        step("collect", trigger=clock()),
                        step("analyze", depends_on=["collect"]),
                        step("report", depends_on=["analyze"]),
                    ],
                ),
            )
        assert store.get_workflow(workflow.id).to_graph() == stored_graph
        assert store.get_workflow(workflow.id).name == workflow.name
        after = {key: task.id for key, task in store.step_tasks(workflow.id).items()}
        assert after == before
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


def test_workflow_definition_cannot_change_during_an_active_execution(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        claimed = store.claim_task_now(tasks["collect"].id, now=NOW)
        assert claimed is not None
        assert claimed.run.workflow_run_id

        edited = store.get_workflow(workflow.id)
        edited.description = "new definition"
        with pytest.raises(ValueError, match="执行中的轮次"):
            store.update_workflow(workflow.id, edited)

        assert store.complete_run(
            tasks["collect"].id,
            claimed.run.id,
            finished_at=NOW + timedelta(seconds=1),
            status="failed",
        )
        workflow_run = store._conn.execute(
            "SELECT status, definition_version FROM workflow_runs WHERE id = ?",
            (claimed.run.workflow_run_id,),
        ).fetchone()
        assert workflow_run["status"] == "failed"
        assert workflow_run["definition_version"] == workflow.version

        updated = store.update_workflow(workflow.id, edited)
        assert updated is not None
        assert updated.version == workflow.version + 1
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


def test_deleting_a_workflow_cancels_queued_and_requests_running_steps(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        running = store.claim_task_now(tasks["collect"].id, now=NOW)
        assert running is not None
        queued = store._enqueue_signal_run_in_transaction(
            tasks["analyze"],
            store.emit_signal("test.delete", now=NOW),
            NOW,
        )
        store._conn.commit()

        store.delete_workflow(workflow.id, now=NOW + timedelta(seconds=1))

        assert store.get_run(tasks["collect"].id, running.run.id).cancel_requested_at
        assert store.get_run(tasks["analyze"].id, queued).status == "cancelled"
        workflow_run = store._conn.execute(
            "SELECT status FROM workflow_runs WHERE id = ?",
            (running.run.workflow_run_id,),
        ).fetchone()
        assert workflow_run["status"] == "cancelled"
    finally:
        store.close()


def test_deleting_a_workflow_takes_its_half_finished_joins(tmp_path):
    """A round that was open when the graph went away must not close later.

    Deleting a workflow leaves its steps behind, disabled.  A half-satisfied
    join left with them is evidence from a graph nobody can open, and nothing
    stops those tasks being switched back on one at a time -- a leftover step
    is deletable and switchable precisely because its workflow is gone.  Turn
    one arm of a fork back on, let it report, and the stale set completes a
    round the other arm never took part in: the join runs on a report from
    before the deletion, and reads output from a round that is over.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(fork_workflow())
        tasks = store.step_tasks(workflow.id)
        join_id = tasks["join"].id
        left_signal = task_signal_name(tasks["left"].id, RUN_SUCCESS_STATUS)

        # One arm of the fork reports, so the join is one name short.
        store.emit_signal(left_signal, source="manual")
        store.deliver_signals(now=NOW, limit=10)
        assert store._satisfied_joins(join_id) == {left_signal}

        store.delete_workflow(workflow.id)
        assert store._satisfied_joins(join_id) == set()
        assert store._join_arrivals(join_id) == {}

        # The leftover steps can still be switched on individually, and that
        # must not be enough to close the round the deletion interrupted.
        store.set_enabled(join_id, True)
        store.set_enabled(tasks["left"].id, True)
        store.emit_signal(left_signal, source="manual")
        store.deliver_signals(now=NOW, limit=10)
        assert store.list_runs(join_id) == []
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

        assert [run.status for run in store.list_runs(tasks["analyze"].id)] == [
            "failed"
        ]
        # Both of the steps below it, not only the adjacent one.
        assert [run.status for run in store.list_runs(tasks["review"].id)] == [
            "skipped"
        ]
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


def test_interleaved_workflow_rounds_do_not_cross_complete_a_join(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(fork_workflow())
        tasks = store.step_tasks(workflow.id)
        left_name = task_signal_name(tasks["left"].id, RUN_SUCCESS_STATUS)
        right_name = task_signal_name(tasks["right"].id, RUN_SUCCESS_STATUS)

        arrivals = [
            (left_name, "round-1", "left"),
            (left_name, "round-2", "left"),
            (right_name, "round-2", "right"),
            (right_name, "round-1", "right"),
        ]
        for index, (name, workflow_run_id, step_key) in enumerate(arrivals):
            store.emit_signal(
                name,
                {
                    "workflow_id": workflow.id,
                    "workflow_run_id": workflow_run_id,
                    "step_key": step_key,
                },
                now=NOW + timedelta(seconds=index),
            )
            store.deliver_signals(now=NOW + timedelta(seconds=index))

        runs = store.list_runs(tasks["join"].id)
        assert len(runs) == 2
        by_round = {run.workflow_run_id: run for run in runs}
        assert set(by_round) == {"round-1", "round-2"}
        for workflow_run_id, run in by_round.items():
            payloads = [item["payload"] for item in run.config_snapshot["signals"]]
            assert {item["step_key"] for item in payloads} == {"left", "right"}
            assert {item["workflow_run_id"] for item in payloads} == {workflow_run_id}
    finally:
        store.close()


def test_two_failures_in_one_round_record_one_skipped_join(tmp_path):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(fork_workflow())
        tasks = store.step_tasks(workflow.id)
        start = store.claim_task_now(tasks["start"].id, now=NOW)
        assert start is not None
        assert store.complete_run(
            tasks["start"].id,
            start.run.id,
            finished_at=NOW + timedelta(seconds=1),
            status=RUN_SUCCESS_STATUS,
        )
        store._conn.execute(
            "UPDATE scheduled_tasks SET next_run_at = NULL WHERE id = ?",
            (tasks["start"].id,),
        )
        store._conn.commit()
        store.deliver_signals(now=NOW + timedelta(seconds=2))

        branches = store.claim_due_tasks(
            NOW + timedelta(seconds=3), limit=10, lease_seconds=30
        )
        assert {item.task.step_key for item in branches} == {"left", "right"}
        for index, item in enumerate(branches):
            assert store.complete_run(
                item.task.id,
                item.run.id,
                finished_at=NOW + timedelta(seconds=4 + index),
                status="failed",
            )

        skipped = store.list_runs(tasks["join"].id)
        assert len(skipped) == 1
        assert skipped[0].status == RUN_SKIPPED_STATUS
        assert skipped[0].workflow_run_id == start.run.workflow_run_id
    finally:
        store.close()


def test_an_arrival_from_an_upstream_that_is_gone_does_not_reach_the_run(tmp_path):
    """Editing a graph is what pulls a join's two halves apart.

    ``satisfied`` was already intersected with the names the trigger waits
    for; ``arrivals`` was not, so a payload from a step that used to be
    upstream survived the edit and was handed to the run as an upstream
    report.  The step below would read output from a round that was over,
    produced by a step no longer above it.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(fork_workflow())
        tasks = store.step_tasks(workflow.id)
        join_id = tasks["join"].id
        required = signal_names(store.get_task(join_id).trigger)
        stale = "task:gone:succeeded"
        store._conn.execute(
            "INSERT INTO signal_joins "
            "(task_id, satisfied_json, arrivals_json, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (
                join_id,
                json.dumps([stale]),
                json.dumps({stale: {"step_key": "gone", "output_path": "/tmp/old"}}),
                NOW.isoformat(),
            ),
        )
        store._conn.commit()

        make_due(store, tasks["start"].id)
        run_rounds(make_handoff_service(store, tmp_path / "output"), 6)

        snapshot = store.list_runs(join_id)[0].config_snapshot
        names = [item["name"] for item in snapshot["signals"]]
        assert stale not in names
        assert sorted(names) == sorted(required)
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


def _as_step_of(
    workflow_id: str, step_key: str, task_id: str, budget: int | None = None
):
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

        claimed = store.claim_task_now(tasks["join"].id, now=NOW + timedelta(hours=1))

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
                            {
                                "name": "task:ghost:succeeded",
                                "payload": {"step_key": "ghost"},
                            }
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
        run = [
            item
            for item in store.list_runs(tasks["analyze"].id)
            if item.status == "succeeded"
        ][-1]
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
        token = _as_step_of(workflow.id, "analyze", tasks["analyze"].id, budget=200000)
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
            [
                {
                    "name": "read_file",
                    "description": "read",
                    "input_schema": {"type": "object"},
                }
            ],
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


def test_an_automatic_retry_keeps_the_upstreams_the_run_was_told_about(tmp_path):
    """The retry queue is the one road to a run that does not pass through the
    claim that derives arrivals -- it copies the failed run's own snapshot.

    That only works because the claim filled it in first, so the two halves
    have to hold together: a manual run gets its upstreams from the graph, and
    the retry of that run inherits them rather than starting from nothing.
    """
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
        # End it the way a failure does: terminal, and the task released.
        store._conn.execute(
            "UPDATE scheduled_task_runs SET status = 'failed' WHERE id = ?",
            (claimed.run.id,),
        )
        store._conn.execute(
            "UPDATE scheduled_tasks SET active_run_id = NULL WHERE id = ?",
            (tasks["analyze"].id,),
        )
        store._conn.commit()

        queued = store.enqueue_retry(
            tasks["analyze"].id,
            claimed.run.id,
            retry_at=NOW + timedelta(hours=2),
        )

        assert queued is not None
        assert queued.trigger_source == "automatic_retry"
        signals = queued.config_snapshot["signals"]
        assert [item["payload"]["step_key"] for item in signals] == ["collect"]
    finally:
        store.close()


def test_workflow_retry_does_not_fail_or_skip_the_chain_before_attempts_exhausted(
    tmp_path,
):
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        failed_at = NOW + timedelta(hours=1)
        claimed = store.claim_task_now(tasks["analyze"].id, now=failed_at)

        assert claimed is not None
        assert store.complete_run(
            tasks["analyze"].id,
            claimed.run.id,
            finished_at=failed_at,
            status="failed",
            error="temporary",
            retry_at=failed_at + timedelta(seconds=30),
        )

        analyze_runs = store.list_runs(tasks["analyze"].id)
        assert [run.status for run in analyze_runs] == ["failed", "queued"]
        assert store.list_runs(tasks["publish"].id) == []

        retry = store.claim_due_tasks(
            failed_at + timedelta(seconds=30), lease_seconds=30
        )[0]
        assert retry.run.task_id == tasks["analyze"].id
        assert retry.run.attempt == 2
        assert store.complete_run(
            tasks["analyze"].id,
            retry.run.id,
            finished_at=failed_at + timedelta(seconds=31),
            status="succeeded",
            summary="recovered",
        )

        store.deliver_signals(now=failed_at + timedelta(seconds=32))
        publish_runs = store.list_runs(tasks["publish"].id)
        assert len(publish_runs) == 1
        assert publish_runs[0].status == "queued"
    finally:
        store.close()


# ── 12. The tools that build a graph ────────────────────────────────────────
#
# The tests above drive the store; the agent drives the tools.  A tool that
# refuses what the store's validator would refuse is still not the same
# guarantee as the validator itself: the tool decides *what to build* before
# the store ever sees it, so a tool that quietly drops a field the validator
# would have flagged hides the problem below the layer that answers for it.
# These tests hold the tool to the same refusals, from the caller's side.


def _chain_steps() -> list[dict]:
    """A three-step chain, in the shape the ``workflow_create`` tool takes.

    The entry step's moment is read off the wall clock rather than written
    down: a one-off that is being *created* has to be in the future, and a
    frozen literal silently stops being one the day it passes.
    """
    return [
        {
            "key": "collect",
            "name": "收集",
            "action_type": "agent_task",
            "instruction": "collect the numbers",
            "trigger_type": "once",
            "at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        },
        {
            "key": "analyze",
            "name": "分析",
            "action_type": "agent_task",
            "instruction": "analyze the numbers",
            "depends_on": ["collect"],
        },
        {
            "key": "publish",
            "name": "发布",
            "action_type": "message",
            "message_text": "the report is ready",
            "depends_on": ["analyze"],
        },
    ]


def test_workflow_create_materializes_each_step_as_a_task(tmp_path):
    """The chain the tool answers with is the chain that actually exists.

    Every step needs a task behind it -- a workflow row with no tasks looks
    like a plan and runs nothing -- and each dependent step's trigger has to
    name its upstreams' success, because "they finished" is a weaker promise
    than "they worked".
    """
    store = make_store(tmp_path)
    try:
        tools = _handoff_tools(store, tmp_path)
        result = tools._workflow_create("nightly report", _chain_steps())

        assert result["ok"] is True
        workflow = result["workflow"]
        assert [item["key"] for item in workflow["steps"]] == [
            "collect",
            "analyze",
            "publish",
        ]
        assert all(item["task_id"] for item in workflow["steps"])

        tasks = store.step_tasks(workflow["id"])
        assert set(tasks) == {"collect", "analyze", "publish"}
        # The entry step keeps the trigger it was given.
        assert tasks["collect"].trigger.trigger_type == "once"
        # A dependent step waits on its upstream's *success*, and on all of
        # them, not on "it stopped running".
        assert tasks["analyze"].trigger.trigger_type == "signal"
        assert tasks["analyze"].trigger.payload["names"] == [
            task_signal_name(tasks["collect"].id, RUN_SUCCESS_STATUS)
        ]
        assert tasks["analyze"].trigger.payload["mode"] == "all"
        assert tasks["publish"].trigger.payload["names"] == [
            task_signal_name(tasks["analyze"].id, RUN_SUCCESS_STATUS)
        ]
    finally:
        store.close()


def test_workflow_create_refuses_a_dependent_step_that_also_names_a_trigger(
    tmp_path,
):
    """The silent-drop regression: the field is refused, not discarded.

    The graph validator refuses a step with both edges and a clock, but it
    can only judge what the tool builds.  An earlier version of the tool
    quietly discarded a dependent step's trigger fields and answered
    ``ok=True``, so the validator never saw the contradiction: the caller
    asked for "run this daily at 10" and got a step that runs at no
    particular time, with nothing said about the difference.  That is why
    this test drives the tool rather than the validator -- the validator's
    own test cannot fail this way.
    """
    store = make_store(tmp_path)
    try:
        tools = _handoff_tools(store, tmp_path)
        steps = _chain_steps()
        steps[1]["trigger_type"] = "daily"
        steps[1]["time_of_day"] = "10:00"

        with pytest.raises(ValueError) as error:
            tools._workflow_create("morning chain", steps)

        # Both the step and the fields it should not have written are named,
        # because "步骤「analyze」既有上游（collect）" is what tells the caller
        # which of their steps to fix.
        assert "步骤「analyze」" in str(error.value)
        assert "触发方式由上游决定" in str(error.value)
        # Refused before anything was written: a graph half-created is a
        # plan in the database that runs part of itself.
        assert store.list_workflows() == []
        assert store.list_tasks() == []
    finally:
        store.close()


def test_workflow_create_refuses_a_chain_with_no_entry_at_all(tmp_path):
    """A step with neither a trigger nor an upstream has no answer to "when".

    The tool could have defaulted it to something -- a clock, a signal --
    but any default would be a schedule the caller never asked for, running
    a prompt they did write.  Refusal is the honest answer.
    """
    store = make_store(tmp_path)
    try:
        tools = _handoff_tools(store, tmp_path)
        steps = [
            {
                "key": "collect",
                "action_type": "message",
                "message_text": "the report is ready",
            }
        ]

        with pytest.raises(ValueError) as error:
            tools._workflow_create("orphan", steps)

        assert "collect" in str(error.value)
        # Worded as the interface words it.  Both surfaces put this refusal in
        # front of the same person, and two spellings of one refusal read as two
        # different rules -- the store's own wording ("必须自带触发方式") is the
        # backstop below them, not the sentence anyone is meant to see.
        assert "没有上游" in str(error.value)
        assert "必须指定触发方式" in str(error.value)
        assert store.list_workflows() == []
        assert store.list_tasks() == []
    finally:
        store.close()


def test_workflow_create_refuses_a_cycle_before_anything_is_written(tmp_path):
    store = make_store(tmp_path)
    try:
        tools = _handoff_tools(store, tmp_path)
        # A pure ring: every step has an upstream, so no step is an entry and
        # none carries a trigger.  Nothing about it contradicts itself; it
        # simply never starts -- which is what the cycle check exists to say.
        ring = [
            {
                "key": "collect",
                "action_type": "message",
                "message_text": "never",
                "depends_on": ["publish"],
            },
            {
                "key": "analyze",
                "action_type": "message",
                "message_text": "never",
                "depends_on": ["collect"],
            },
            {
                "key": "publish",
                "action_type": "message",
                "message_text": "never",
                "depends_on": ["analyze"],
            },
        ]

        with pytest.raises(ValueError) as error:
            tools._workflow_create("ring", ring)

        # The ring itself, not just the fact of one: "which steps" is the
        # actionable part of the message.
        assert "collect → publish → analyze → collect" in str(error.value)
        assert store.list_workflows() == []
        assert store.list_tasks() == []
    finally:
        store.close()


def test_workflow_create_refuses_a_graph_with_no_steps(tmp_path):
    store = make_store(tmp_path)
    try:
        tools = _handoff_tools(store, tmp_path)
        with pytest.raises(ValueError) as error:
            tools._workflow_create("empty", [])
        assert "至少要有一个步骤" in str(error.value)
        assert store.list_workflows() == []
    finally:
        store.close()


def test_workflow_create_carries_a_steps_acceptance_into_its_task(tmp_path):
    """A criterion is part of the step, so it has to arrive with the task.

    The response says it out loud for the same reason the store keeps it:
    a criterion nobody was told about is indistinguishable from no
    criterion, and the difference decides whether a failed run looks like a
    bug or like the criterion doing its job.
    """
    store = make_store(tmp_path)
    try:
        tools = _handoff_tools(store, tmp_path)
        steps = _chain_steps()
        steps[1]["criteria"] = ["the numbers add up"]
        steps[1]["verify_command"] = "true"

        result = tools._workflow_create("checked chain", steps)

        workflow = result["workflow"]
        assert workflow["steps"][1]["acceptance"] == {
            "criteria": ["the numbers add up"],
            "verify_command": "true",
        }
        # And the task, not just the description of it.
        tasks = store.step_tasks(workflow["id"])
        assert tasks["analyze"].acceptance.criteria == ["the numbers add up"]
        assert tasks["analyze"].acceptance.verify_command == "true"
        # The other steps were not handed a criterion they never asked for.
        assert tasks["collect"].acceptance.criteria == []
        assert "判定成功的依据" in result["summary_text"]
    finally:
        store.close()


def test_workflow_list_shows_the_steps_and_their_edges(tmp_path):
    store = make_store(tmp_path)
    try:
        tools = _handoff_tools(store, tmp_path)
        created = tools._workflow_create("nightly report", _chain_steps())

        listed = tools._workflow_list()

        assert listed["ok"] is True
        assert listed["count"] == 1
        item = listed["items"][0]
        assert item["id"] == created["workflow"]["id"]
        assert item["name"] == "nightly report"
        assert [(s["key"], s["depends_on"]) for s in item["steps"]] == [
            ("collect", []),
            ("analyze", ["collect"]),
            ("publish", ["analyze"]),
        ]
    finally:
        store.close()


def test_workflow_delete_disables_the_steps_and_keeps_the_runs(tmp_path):
    """Deleting a workflow stops it; it does not unrun it.

    The tasks are disabled and the history stays, because somebody deleting
    a chain is stopping it -- not asking to forget that it ran.  The run
    rows are the record that outlives the graph.
    """
    store = make_store(tmp_path)
    try:
        tools = _handoff_tools(store, tmp_path)
        created = tools._workflow_create("nightly report", _chain_steps())
        workflow_id = created["workflow"]["id"]
        tasks = store.step_tasks(workflow_id)
        make_due(store, tasks["collect"].id)
        run_rounds(make_handoff_service(store, tmp_path / "output"), 4)
        assert store.list_runs(tasks["collect"].id)

        result = tools._workflow_delete(workflow_id)

        assert result["ok"] is True
        assert result["deleted"] is True
        assert result["disabled_task_ids"] == sorted(task.id for task in tasks.values())
        assert store.list_workflows() == []
        # The tasks still exist, but can no longer fire.
        after = {task.id: task for task in store.list_tasks()}
        assert all(
            task.id in after and after[task.id].enabled is False
            for task in tasks.values()
        )
        # The history was not taken with it.
        assert store.list_runs(tasks["collect"].id)
        assert store.list_runs(tasks["analyze"].id)
    finally:
        store.close()


# ── 13. A step is a step only while its graph exists ───────────────────────
#
# The refusals that send a step's owner to the workflow -- do not delete one
# step, do not throw one step's switch -- are all about a graph that can still
# be saved: deleting a step outright leaves the steps below it subscribed to a
# signal nobody emits any more, and the next save of the workflow rebuilds the
# step under a new id.  Deleting a workflow takes that graph away and leaves
# its steps behind on purpose, and everything those refusals are protecting
# stopped existing with it.  A refusal that outlives its reason is not caution;
# it is a row nobody can remove from any surface.


def test_a_deleted_workflows_steps_can_then_be_deleted(tmp_path):
    """The leftovers are the record that it ran, and the record is removable.

    Which is the whole point of keeping them: whoever wants the history keeps
    it, and whoever is done with it can say so.  There used to be no way to
    say so.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        store.delete_workflow(workflow.id)

        for task in tasks.values():
            store.delete_task(task.id)

        assert store.list_tasks() == []
    finally:
        store.close()


def test_a_live_workflows_step_refuses_deletion_and_its_own_switch(tmp_path):
    """While the graph is there, both answers are still the graph's.

    The refusal has to come from the store rather than from one caller,
    because every surface asks the same question and a copy of the rule in
    each is how the answer drifts -- the interface refused these two and the
    agent's own tool deleted the step without comment.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        analyze = tasks["analyze"]

        with pytest.raises(ValueError) as refused_delete:
            store.delete_task(analyze.id)
        assert "流程中的步骤" in str(refused_delete.value)

        with pytest.raises(ValueError) as refused_switch:
            store.set_enabled(analyze.id, False)
        assert "请暂停整个流程" in str(refused_switch.value)

        # Refused, and nothing moved: the step is still there and still on.
        remaining = {task.id for task in store.list_tasks()}
        assert {task.id for task in tasks.values()} <= remaining
        assert store.step_tasks(workflow.id)["analyze"].enabled is True
    finally:
        store.close()


def test_a_step_is_deletable_the_moment_its_workflow_is_gone(tmp_path):
    """Membership survives the graph, and is not what the refusals turn on.

    The step task still says which workflow it came from -- that is how the
    list can explain what it is looking at -- but the question the refusals
    ask is whether that workflow still exists, not whether it was ever named.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        analyze = tasks["analyze"]

        store.delete_workflow(workflow.id)

        after = store.get_task(analyze.id)
        assert after is not None
        assert after.workflow_id == workflow.id
        assert after.step_key == "analyze"
        store.delete_task(analyze.id)
        assert store.get_task(analyze.id) is None
    finally:
        store.close()


def test_a_running_task_refuses_to_be_deleted(tmp_path):
    """Its completion writes back to the row this would remove.

    Left to run, the deletion turns into an "owned run disappeared" error in
    the scheduler thread -- a failure nobody asked for, in place of the
    cancellation the person can still make.
    """
    store = make_store(tmp_path)
    try:
        task = store.create_task(
            NewScheduledTask(
                name="collect",
                kind="agent_prompt",
                trigger=clock(),
                payload={"prompt": "do collect"},
                delivery_mode="standalone",
                delivery_target=DeliveryTarget.standalone(),
            )
        )
        assert store.claim_task_now(task.id, now=NOW) is not None

        with pytest.raises(ValueError) as refused:
            store.delete_task(task.id)
        assert "正在运行" in str(refused.value)
        assert store.get_task(task.id) is not None
    finally:
        store.close()


def test_the_delete_tool_refuses_a_live_step_and_takes_an_orphan(tmp_path):
    """What the agent may delete is the store's answer, from the caller's side.

    A tool that deleted a live step would break the chain and see the step
    rebuilt by the next save -- the failure the store exists to refuse -- and
    a tool that kept refusing a step left behind by a deleted workflow would
    make the leftover unreachable from the conversation that could remove it.
    """
    store = make_store(tmp_path)
    try:
        tools = _handoff_tools(store, tmp_path)
        created = tools._workflow_create("nightly report", _chain_steps())
        workflow_id = created["workflow"]["id"]
        tasks = store.step_tasks(workflow_id)

        with pytest.raises(ValueError) as refused:
            tools._schedule_delete(tasks["analyze"].id)
        assert "流程中的步骤" in str(refused.value)
        assert store.get_task(tasks["analyze"].id) is not None

        tools._workflow_delete(workflow_id)
        result = tools._schedule_delete(tasks["analyze"].id)
        assert result["ok"] is True
        assert result["deleted"] is True
        assert store.get_task(tasks["analyze"].id) is None
    finally:
        store.close()


def test_a_chain_records_the_sentence_that_asked_for_it(tmp_path):
    """A chain, asked for by one sentence, carries that sentence on every step.

    A step is not asked for by name -- the chain is -- so the step rows quote
    the chain's words.  A step that said "asked for by nobody" would be
    indistinguishable from a task that appeared without being asked for, which
    is the thing this column exists to tell apart.
    """
    store = make_store(tmp_path)
    try:
        tools = _handoff_tools(store, tmp_path)
        created = tools._workflow_create(
            "nightly report",
            _chain_steps(),
            intent="每天跑一遍收集、分析、发布这三步",
        )
        workflow = store.get_workflow(created["workflow"]["id"])
        tasks = store.step_tasks(created["workflow"]["id"])

        assert workflow is not None
        assert workflow.request_quote == "每天跑一遍收集、分析、发布这三步"
        assert sorted(tasks) == ["analyze", "collect", "publish"]
        assert all(
            task.request_quote == "每天跑一遍收集、分析、发布这三步"
            for task in tasks.values()
        )
    finally:
        store.close()


def test_editing_a_chain_keeps_who_asked_for_it(tmp_path):
    """Rewriting a step does not change who asked for the chain.

    The edit form a caller rebuilds the graph from has no quote on it, so the
    update path must not write the column: doing so would blank the only
    evidence the chain has about its own origin.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(
            Workflow(
                name="nightly report",
                steps=[
                    step("collect", trigger=clock()),
                    step("publish", depends_on=["collect"]),
                ],
                request_quote="每天跑一遍收集和发布这两步",
            )
        )
        edited = Workflow(
            id=workflow.id,
            name="nightly report (v2)",
            steps=[
                step("collect", trigger=clock(), name="收集（改）"),
                step("publish", depends_on=["collect"]),
            ],
        )

        store.update_workflow(workflow.id, edited)
        reread = store.get_workflow(workflow.id)
        tasks = store.step_tasks(workflow.id)

        assert reread is not None
        assert reread.name == "nightly report (v2)"
        assert reread.request_quote == "每天跑一遍收集和发布这两步"
        assert tasks["collect"].name == "收集（改）"
        assert tasks["collect"].request_quote == "每天跑一遍收集和发布这两步"
    finally:
        store.close()


TERMINAL_RUN_STATUSES = {"succeeded", "failed", "interrupted", "cancelled"}


async def _await_step_runs_settled(store, tasks, *, timeout: float = 8.0) -> None:
    """Wait until every named step has a run and none of them is still going.

    Polling the rows rather than awaiting an executor-side event, because the
    interesting failure is "the step never ran at all": an event that is never
    set would hang, and a hang is not a failing test.
    """

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        runs = [store.list_runs(tasks[key].id) for key in ("collect", "analyze", "publish")]
        if all(run and run[0].status in TERMINAL_RUN_STATUSES for run in runs):
            return
        await asyncio.sleep(0.01)
    settled = {
        key: [run.status for run in store.list_runs(tasks[key].id)]
        for key in ("collect", "analyze", "publish")
    }
    raise AssertionError(f"steps did not settle within {timeout}s: {settled}")


def test_a_step_runs_when_its_upstream_finishes_not_at_the_next_poll(tmp_path):
    """A hop costs a step, not a poll interval.

    A step's successor is started by a signal, and signals are delivered at the
    top of a tick -- so with a fixed ``sleep(poll_seconds)`` between ticks, every
    hop of every chain pays the full interval, on top of whatever the step
    itself took.  ``poll_seconds`` here is an hour, so the only way ``publish``
    can run at all is if the loop is woken by ``analyze`` finishing.  The tests
    above drive ``run_once`` by hand and therefore never exercised the sleep;
    this timeout is what turns the old behaviour into a failure rather than a
    hang.
    """
    import contextlib

    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["collect"].id)

        published = asyncio.Event()

        async def executor(task, run):
            if task.name == "publish":
                published.set()
            return ExecutionResult(
                summary=f"ran {task.name}", text_output=f"out {task.name}"
            )

        async def unused(*args, **kwargs):
            raise AssertionError("system executor should not be called")

        async def delivery(task, run, result):
            return "delivered"

        async def scenario():
            service = SchedulerService(
                store=store,
                agent_executor=executor,
                system_executor=unused,
                delivery=delivery,
                poll_seconds=3600,
            )
            loop_task = asyncio.create_task(service.run_forever())
            try:
                await asyncio.wait_for(published.wait(), timeout=8)
                # ``published`` is set from inside publish's executor, which is
                # before that run has recorded anything.  Stopping the loop
                # there would cut the run off mid-bookkeeping -- the status
                # would say "scheduler stopped" and the assertions below would
                # be measuring the harness.  Waiting for the rows also makes the
                # claim stronger: all three steps *succeeded*, and they did it
                # without a poll.
                await _await_step_runs_settled(store, tasks)
            finally:
                loop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await loop_task
                await service.shutdown()

        asyncio.run(scenario())

        for key in ("collect", "analyze", "publish"):
            runs = store.list_runs(tasks[key].id)
            assert len(runs) == 1, (
                f"{key} ran {len(runs)} times: "
                f"{[(r.id, r.status, r.error) for r in runs]}"
            )
            assert runs[0].status == "succeeded", (
                f"{key} ended {runs[0].status!r}: {runs[0].error!r}"
            )
    finally:
        store.close()


# ── Workflow-run bookkeeping gaps found in review ─────────────────────────


def _workflow_run_status(store: SchedulerStore, workflow_run_id: str) -> str:
    row = store._conn.execute(
        "SELECT status FROM workflow_runs WHERE id = ?", (workflow_run_id,)
    ).fetchone()
    return str(row["status"]) if row is not None else ""


def test_complete_run_rejects_a_status_that_is_not_a_run_status(tmp_path):
    """A typo in ``status`` must fail loudly, not fabricate history.

    ``complete_run`` is called across module boundaries (runtime, web channel,
    tools), and the one guard it had -- ``_skip_blocked_steps`` treating every
    non-success as a failure -- turned a one-letter typo (``success`` for
    ``succeeded``) into a cascade of skipped runs and a failed workflow run,
    all silently.  The store knows the legal statuses; it says no.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        claimed = store.claim_task_now(tasks["collect"].id, now=NOW)
        assert claimed is not None

        with pytest.raises(ValueError, match="未知的运行终态"):
            store.complete_run(
                tasks["collect"].id,
                claimed.run.id,
                finished_at=NOW + timedelta(seconds=1),
                status="success",
            )
        # Nothing was written: the run is still running and the round open.
        assert store.list_runs(tasks["collect"].id)[0].status == "running"
        assert _workflow_run_status(store, claimed.run.workflow_run_id) == "running"
    finally:
        store.close()


def test_a_clock_claim_carries_the_workflow_round_it_belongs_to(tmp_path):
    """The claimed task must agree with the row the database holds.

    ``claim_due_tasks`` builds the returned ``TaskRun`` by hand, and the hand
    copy once omitted ``workflow_run_id`` while the row it claims had one --
    exactly the drift its own comment warns about.  The scheduler executes the
    copy; anything the row gained and the copy lost is invisible to it.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["collect"].id)

        claimed = store.claim_due_tasks(
            now=NOW, limit=5, lease_seconds=300
        )
        assert len(claimed) == 1

        row = store._conn.execute(
            "SELECT workflow_run_id, config_snapshot_json FROM scheduled_task_runs "
            "WHERE id = ?",
            (claimed[0].run.id,),
        ).fetchone()
        assert claimed[0].run.workflow_run_id == row["workflow_run_id"]
        snapshot = json.loads(row["config_snapshot_json"])
        assert snapshot.get("workflow_run_id") == row["workflow_run_id"]
        assert claimed[0].run.config_snapshot == snapshot
    finally:
        store.close()


def test_join_progress_shows_the_round_not_the_no_round_leftovers(tmp_path):
    """A workflow join's progress is its round's, not an older stray arrival.

    Arrivals that carry no round (an external emitter naming the same signal)
    are recorded in the round-less bucket, and a join waiting inside a round
    can never be completed by them.  Reporting them as "satisfied" answers the
    wrong question -- it hides the report the round is actually short of, so a
    join that is one upstream away looks like it is waiting for an upstream
    that already reported.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(
            Workflow(
                name="dual entry",
                steps=[
                    step("a", trigger=clock()),
                    step("b", trigger=TriggerSpec.once("2027-01-01T00:00:00+00:00", "UTC")),
                    step("join", depends_on=["a", "b"]),
                ],
            )
        )
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["a"].id)
        due = store.claim_due_tasks(now=NOW, limit=5, lease_seconds=300)
        assert store.complete_run(
            tasks["a"].id,
            due[0].run.id,
            finished_at=NOW + timedelta(seconds=1),
            status="succeeded",
        )
        store.deliver_signals(now=NOW + timedelta(seconds=2))

        # Somebody outside the workflow reports b's success, carrying no round.
        store.emit_signal(
            task_signal_name(tasks["b"].id, "succeeded"),
            {"task_id": tasks["b"].id, "status": "succeeded"},
            now=NOW + timedelta(seconds=3),
        )
        store.deliver_signals(now=NOW + timedelta(seconds=4))

        progress = store.join_progress(tasks["join"].id)
        assert progress["satisfied"] == [
            task_signal_name(tasks["a"].id, "succeeded")
        ]
        assert progress["missing"] == [
            task_signal_name(tasks["b"].id, "succeeded")
        ]
    finally:
        store.close()


def test_a_stale_claim_settles_the_workflow_run_it_interrupted(tmp_path):
    """Recovery, not only completion, has to close a workflow execution.

    ``complete_run`` is where a workflow run is rolled up, but it is not the
    only way a run ends: a scheduler that dies mid-run leaves a lease to
    expire, and ``recover_stale_runs`` is what marks that run ``interrupted``.
    If the roll-up lives only in ``complete_run``, every crashed entry step
    leaves its workflow run ``running`` for ever -- and ``update_workflow``
    refuses to edit a definition while any run of it is ``running``, so one
    crash would lock the definition until the whole workflow was deleted.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        claimed = store.claim_task_now(tasks["collect"].id, now=NOW)
        assert claimed is not None

        store.recover_stale_runs(now=NOW + timedelta(minutes=10))

        assert _workflow_run_status(store, claimed.run.workflow_run_id) in {
            "interrupted",
            "failed",
        }
        # The definition is editable again: the round is over, so nothing of
        # the old definition is still executing.
        edited = store.get_workflow(workflow.id)
        edited.description = "after crash"
        assert store.update_workflow(workflow.id, edited) is not None
    finally:
        store.close()


def test_a_released_claim_settles_the_workflow_run_it_released(tmp_path):
    """``release_claim`` ends a run too, so it has to roll up the round as well.

    The scheduler releases a claim when it stops before the slot opens, and a
    released run is left ``interrupted`` with nobody completing it afterwards.
    Same consequence as a stale lease: the round stays ``running`` for ever.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        claimed = store.claim_task_now(tasks["collect"].id, now=NOW)
        assert claimed is not None

        assert store.release_claim(
            tasks["collect"].id,
            claimed.run.id,
            now=NOW + timedelta(seconds=1),
            reason="scheduler stopped",
        )

        assert _workflow_run_status(store, claimed.run.workflow_run_id) in {
            "interrupted",
            "failed",
        }
    finally:
        store.close()


def test_a_dormant_round_does_not_lock_the_definition_for_ever(tmp_path):
    """A round whose remaining steps are merely not coming must not block edits.

    A workflow with two clock entries runs one of them; the other entry's
    occurrence is weeks away, so the round will not complete on its own any
    time soon.  ``update_workflow`` may refuse while a round is genuinely in
    progress, but a round with no queued or running work left is not in
    progress -- it is finished for practical purposes, and leaving it
    ``running`` converts one manual run into a permanent edit lock.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(
            Workflow(
                name="dual entry",
                steps=[
                    step("a", trigger=clock()),
                    step("b", trigger=TriggerSpec.once("2027-01-01T00:00:00+00:00", "UTC")),
                    step("join", depends_on=["a", "b"]),
                ],
            )
        )
        tasks = store.step_tasks(workflow.id)
        claimed = store.claim_task_now(tasks["a"].id, now=NOW)
        assert claimed is not None
        assert store.complete_run(
            tasks["a"].id,
            claimed.run.id,
            finished_at=NOW + timedelta(seconds=1),
            status="succeeded",
        )

        edited = store.get_workflow(workflow.id)
        edited.description = "one entry ran, the other never will"
        updated = store.update_workflow(workflow.id, edited)

        assert updated is not None
        assert updated.description == edited.description
    finally:
        store.close()


def test_running_a_join_upstream_now_joins_the_round_waiting_for_it(tmp_path):
    """A manual run of a join upstream feeds the round, not a new round.

    ``run_now``/``claim_task_now`` on a step whose join is mid-round used to
    mint a fresh workflow-run id, so its success signal carried the new id and
    the waiting join never heard it: the round it belongs to stalls for ever
    while a second, empty round is created around the manual run.  The manual
    run has to inherit the round its downstream join is waiting on.

    A dual-entry workflow makes the round deterministic: ``a`` fires, its
    report opens the join's round, and ``b`` -- whose clock is a week out --
    is what the user runs by hand.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(
            Workflow(
                name="dual entry",
                steps=[
                    step("a", trigger=clock()),
                    step("b", trigger=TriggerSpec.once("2027-01-01T00:00:00+00:00", "UTC")),
                    step("join", depends_on=["a", "b"]),
                ],
            )
        )
        tasks = store.step_tasks(workflow.id)
        # Run ``a`` off its clock, which is what consumes the occurrence: a
        # manual claim deliberately does not, and a leftover occurrence would
        # fire a second round of its own and muddy what is being measured.
        make_due(store, tasks["a"].id)
        due = store.claim_due_tasks(now=NOW, limit=5, lease_seconds=300)
        assert [item.task.step_key for item in due] == ["a"]
        round_id = due[0].run.workflow_run_id
        assert round_id
        assert store.complete_run(
            tasks["a"].id,
            due[0].run.id,
            finished_at=NOW + timedelta(seconds=1),
            status="succeeded",
        )
        store.deliver_signals(now=NOW + timedelta(seconds=2))

        # The join heard from a and is short b, all inside one round.
        progress = store.join_progress(tasks["join"].id)
        assert progress["missing"] == [task_signal_name(tasks["b"].id, "succeeded")]

        # The user runs b now, weeks before its clock.
        running = store.claim_task_now(tasks["b"].id, now=NOW + timedelta(seconds=3))
        assert running is not None
        assert running.run.workflow_run_id == round_id

        assert store.complete_run(
            tasks["b"].id,
            running.run.id,
            finished_at=NOW + timedelta(seconds=4),
            status="succeeded",
        )
        store.deliver_signals(now=NOW + timedelta(seconds=5))
        claimed = store.claim_due_tasks(
            now=NOW + timedelta(seconds=6), limit=5, lease_seconds=300
        )
        join_claims = [c for c in claimed if c.task.step_key == "join"]
        assert len(join_claims) == 1
        assert join_claims[0].run.workflow_run_id == round_id
        # And no second round was minted around the manual run.
        rounds = store._conn.execute(
            "SELECT id FROM workflow_runs WHERE workflow_id = ?",
            (workflow.id,),
        ).fetchall()
        assert len(rounds) == 1
    finally:
        store.close()


def test_the_second_clock_entry_joins_the_round_waiting_for_it(tmp_path):
    """Two clock entries meeting at a join share one round.

    Each entry firing is normally its own round, but a join that is already
    waiting on this step cannot be completed by a round the step was not part
    of: the report it sends carries the new round's id, and the waiting join
    never hears it.  So a clock firing pairs with the round that is waiting
    for it -- which is what makes a fork-join built from two schedules work at
    all, instead of leaving both rounds permanently one report short.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(
            Workflow(
                name="dual clock",
                steps=[
                    step("a", trigger=clock()),
                    step("b", trigger=clock("2026-05-01T11:58:00+00:00")),
                    step("join", depends_on=["a", "b"]),
                ],
            )
        )
        tasks = store.step_tasks(workflow.id)
        # a's occurrence is placed ahead of b's so `limit=1` picks a: the
        # scenario needs a to open the round and b to join it later.
        make_due(store, tasks["a"].id, when=NOW - timedelta(minutes=5))
        first = store.claim_due_tasks(now=NOW, limit=1, lease_seconds=300)
        assert [item.task.step_key for item in first] == ["a"]
        round_id = first[0].run.workflow_run_id
        assert store.complete_run(
            tasks["a"].id,
            first[0].run.id,
            finished_at=NOW + timedelta(seconds=1),
            status="succeeded",
        )
        store.deliver_signals(now=NOW + timedelta(seconds=2))

        # b's own clock comes due; it belongs to the round already waiting.
        make_due(store, tasks["b"].id, when=NOW + timedelta(minutes=5))
        second = store.claim_due_tasks(
            now=NOW + timedelta(minutes=5), limit=5, lease_seconds=300
        )
        assert [item.task.step_key for item in second] == ["b"]
        assert second[0].run.workflow_run_id == round_id

        assert store.complete_run(
            tasks["b"].id,
            second[0].run.id,
            finished_at=NOW + timedelta(minutes=5, seconds=1),
            status="succeeded",
        )
        store.deliver_signals(now=NOW + timedelta(minutes=5, seconds=2))
        claimed = store.claim_due_tasks(
            now=NOW + timedelta(minutes=5, seconds=3), limit=5, lease_seconds=300
        )
        join_claims = [item for item in claimed if item.task.step_key == "join"]
        assert len(join_claims) == 1
        assert join_claims[0].run.workflow_run_id == round_id
        rounds = store._conn.execute(
            "SELECT id FROM workflow_runs WHERE workflow_id = ?",
            (workflow.id,),
        ).fetchall()
        assert len(rounds) == 1
    finally:
        store.close()
def test_a_cancelled_step_is_not_recorded_as_a_failure_of_the_chain(tmp_path):
    """Cancel is the user stopping the work, not the work failing.

    A cancelled entry used to be rolled up like a failure: every downstream
    step recorded as skipped with a reason naming a failure, the workflow run
    marked ``failed``, and a ``skipped`` signal broadcast as though the chain
    had broken.  The record should say what happened -- cancelled -- and a
    cancelled round is not a failed one.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        claimed = store.claim_task_now(tasks["collect"].id, now=NOW)
        assert claimed is not None

        assert store.complete_run(
            tasks["collect"].id,
            claimed.run.id,
            finished_at=NOW + timedelta(seconds=1),
            status="cancelled",
            error="cancelled by user",
        )

        analyze_runs = store.list_runs(tasks["analyze"].id)
        assert len(analyze_runs) == 1
        assert analyze_runs[0].status == RUN_SKIPPED_STATUS
        # The reason is where a skipped run's account has always lived: its
        # summary, not its error -- nothing failed, so there is no error.
        assert "取消" in analyze_runs[0].summary
        assert "failed" not in analyze_runs[0].summary
        assert _workflow_run_status(store, claimed.run.workflow_run_id) == "cancelled"
        # The signal for the step itself says cancelled, and the skipped ones
        # say skipped -- what a subscriber hears matches what the record says.
        names = [
            row["name"]
            for row in store._conn.execute(
                "SELECT name FROM signal_emissions ORDER BY rowid"
            ).fetchall()
        ]
        statuses = [parse_task_signal(name)[1] for name in names]
        assert "cancelled" in statuses
        assert statuses.count(RUN_SKIPPED_STATUS) == 2
    finally:
        store.close()



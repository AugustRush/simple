"""Whether a run did the job, as a question separate from whether it ran.

The defect these tests exist for is a collapse.  A run's status used to be
decided by delivery alone, so an answer that arrived and was useless was
recorded as ``succeeded``, and a perfect answer that could not be delivered
was recorded as ``failed`` -- which also retried it, skipped everything
downstream, and raised it for attention.  Three different questions were
being answered with one word.

The three, kept apart:

* ``status`` -- did the run happen?
* ``verdict`` -- did what it produced meet the bar the task was given?
* ``delivery_status`` -- did the result reach anybody?

Two rules fall out of that separation and are the load-bearing ones here:

* an unanswerable check is **not** a failure.  ``rejected``, ``timeout``,
  ``setup_error`` and ``cancelled`` are statements about the *check*;
  folding them into ``failed`` asserts something nobody observed.  They are
  ``unverified``.
* a self-report is weaker evidence than a command, so it can only ever
  *lower* a verdict.  An agent saying "I could not do this" is worth acting
  on; an agent saying "I did this" is not evidence, and must never stand in
  for a check that could not run.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from agent.scheduler import (
    Acceptance,
    DeliveryTarget,
    ExecutionResult,
    NewScheduledTask,
    SchedulerService,
    SchedulerStore,
    TriggerSpec,
    Workflow,
    WorkflowStep,
    run_needs_attention,
)
from agent.scheduler.runtime import RUN_UNVERIFIED_STATUS
from agent.verification import (
    VERDICT_FAILED,
    VERDICT_NONE,
    VERDICT_PASSED,
    VERDICT_UNKNOWN,
    VerificationStatus,
    combine_verdicts,
    verdict_for,
)

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def tmp_path():
    """A scratch directory rooted at ``/tmp``.

    Overrides pytest's own fixture, which fails in this environment: its
    per-run root under the system temp directory cannot be created when the
    session is sandboxed, so every test that asks for ``tmp_path`` errors out
    before it runs.  ``/tmp`` is writable and just as disposable.
    """
    path = Path("/tmp") / f"simple-acceptance-test-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        import shutil

        shutil.rmtree(path, ignore_errors=True)


def make_store(tmp_path: Path) -> SchedulerStore:
    return SchedulerStore(db_path=tmp_path / "scheduler.db")


def clock(at: str = "2026-05-01T11:59:00+00:00") -> TriggerSpec:
    return TriggerSpec.once(at, "UTC")


def make_service(
    store: SchedulerStore,
    *,
    self_report: tuple[str, str] | None = None,
    delivery_status: str = "delivered",
) -> SchedulerService:
    """A service whose agent runs succeed and report nothing, unless told to.

    *self_report* stands in for a run that called ``report_outcome``: the
    executor is what reads the report off the run and hands it to the runtime
    as part of the result, which is exactly what the CLI executor does.  The
    acceptance check itself is never faked -- these tests drive the real
    verifier, so what is being tested is the real mapping from a real exit
    code to a real status.
    """

    async def executor(task, run):
        verdict, reason = self_report or (VERDICT_NONE, "")
        return ExecutionResult(
            summary=f"ran {task.name}",
            text_output=f"out {task.name}",
            self_report_verdict=verdict,
            self_report_reason=reason,
        )

    async def unused(*args, **kwargs):
        raise AssertionError("system executor should not be called")

    async def delivery(task, run, result):
        return delivery_status

    return SchedulerService(
        store=store,
        agent_executor=executor,
        system_executor=unused,
        delivery=delivery,
        poll_seconds=30,
        lease_seconds=300,
    )


def force_acceptance(
    store: SchedulerStore, task_id: str, acceptance: Acceptance
) -> None:
    """Write a criterion into an existing task's row, past the creation gate.

    Needed because the creation gate and the run-time verifier are *supposed*
    to be able to disagree -- that is the whole reason the verifier checks
    again instead of trusting the answer it gave when the task was written.
    A command can become unacceptable between the two moments (a blocked-
    commands entry added, a session confirmation that does not survive to
    3am), and what the runtime does with that is the thing under test here.
    The only way to reach that state from a test is to go around the gate.
    """
    store._conn.execute(
        "UPDATE scheduled_tasks SET acceptance_json = ? WHERE id = ?",
        (acceptance.to_json(), task_id),
    )
    store._conn.commit()


def make_due(store: SchedulerStore, task_id: str, when: datetime = NOW) -> None:
    """Move a task's next occurrence into the past so the clock can claim it."""
    store._conn.execute(
        "UPDATE scheduled_tasks SET next_run_at = ? WHERE id = ?",
        ((when - timedelta(minutes=1)).astimezone(timezone.utc).isoformat(), task_id),
    )
    store._conn.commit()


def make_task(
    store: SchedulerStore,
    *,
    name: str = "nightly",
    workspace_root: str,
    acceptance: Acceptance | None = None,
) -> object:
    return store.create_task(
        NewScheduledTask(
            name=name,
            kind="agent_prompt",
            trigger=clock(),
            payload={"prompt": "do the thing"},
            delivery_mode="standalone",
            delivery_target=DeliveryTarget.standalone(),
            workspace_root=workspace_root,
            acceptance=acceptance or Acceptance(),
        ),
        now=NOW,
    )


async def run_to_terminal(service: SchedulerService, now: datetime = NOW) -> None:
    """One claim-and-complete cycle, with a second pass to settle anything it
    unblocked."""
    for hop in range(3):
        await service.run_once(now=now + timedelta(seconds=30 * hop))


# ── 1. The verdict is a second axis, and delivery is a third ────────────────


def test_a_task_with_no_criterion_still_succeeds_on_delivery(tmp_path):
    """The no-regression case, and it is the majority case.

    Most scheduled tasks have no mechanically checkable outcome.  They must
    behave exactly as they did before this existed: delivered means done.
    """
    store = make_store(tmp_path)
    try:
        task = make_task(store, workspace_root=str(tmp_path))
        make_due(store, task.id)

        asyncio.run(run_to_terminal(make_service(store)))

        run = store.list_runs(task.id)[0]
        assert run.status == "succeeded"
        # Empty, not "passed": nothing was declared, so nothing was judged.
        assert run.verdict == VERDICT_NONE
        assert run.verification is None
    finally:
        store.close()


def test_a_failing_check_makes_the_run_failed_and_keeps_the_output(tmp_path):
    """Exit code, verdict and status, and the evidence that explains them."""
    store = make_store(tmp_path)
    try:
        task = make_task(
            store,
            workspace_root=str(tmp_path),
            acceptance=Acceptance(
                criteria=["the tests pass"], verify_command="false"
            ),
        )
        make_due(store, task.id)

        asyncio.run(run_to_terminal(make_service(store)))

        run = store.list_runs(task.id)[0]
        assert run.status == "failed"
        assert run.verdict == VERDICT_FAILED
        assert run.verification is not None
        assert run.verification.status is VerificationStatus.FAILED
        assert run.verification.exit_code == 1
        # The reason travels in the run's own error field, so a reader does
        # not have to open the verification record to learn why.
        assert "验收命令未通过" in run.error
        assert "退出码 1" in run.error
    finally:
        store.close()


def test_a_passing_check_makes_the_run_succeeded(tmp_path):
    store = make_store(tmp_path)
    try:
        task = make_task(
            store,
            workspace_root=str(tmp_path),
            acceptance=Acceptance(criteria=["always true"], verify_command="true"),
        )
        make_due(store, task.id)

        asyncio.run(run_to_terminal(make_service(store)))

        run = store.list_runs(task.id)[0]
        assert run.status == "succeeded"
        assert run.verdict == VERDICT_PASSED
        assert run.verification is not None
        assert run.verification.status is VerificationStatus.PASSED
        # A pass needs no excuse, so it does not grow an error line.
        assert run.error == ""
    finally:
        store.close()


def test_a_run_that_delivered_nothing_is_failed_even_when_the_check_passed(tmp_path):
    """Delivery is its own axis: the work was right and nobody got it."""
    store = make_store(tmp_path)
    try:
        task = make_task(
            store,
            workspace_root=str(tmp_path),
            acceptance=Acceptance(criteria=["always true"], verify_command="true"),
        )
        make_due(store, task.id)

        asyncio.run(run_to_terminal(make_service(store, delivery_status="failed")))

        run = store.list_runs(task.id)[0]
        assert run.status == "failed"
        # The verdict still says what it says about the work. Losing the
        # verdict because delivery broke would throw away the only part of
        # this that is hard to reconstruct.
        assert run.verdict == VERDICT_PASSED
        assert "unexpected delivery status" in run.error
    finally:
        store.close()


# ── 2. "We could not tell" is not "it was wrong" ────────────────────────────


def test_a_refused_check_is_unverified_rather_than_failed(tmp_path):
    """The command was never allowed to run, so nothing was observed.

    Recording this as ``failed`` would be a claim about the work, and there
    is no evidence for it; recording it as ``succeeded`` would be worse.
    """
    store = make_store(tmp_path)
    try:
        # Written past the creation gate, because the gate is not what this
        # test is about: it is about what happens when a command that was
        # acceptable becomes unacceptable between the task being written and
        # the run -- which is the whole reason the verifier checks again.
        task = make_task(store, workspace_root=str(tmp_path))
        force_acceptance(
            store,
            task.id,
            Acceptance(criteria=["something checkable"], verify_command="rm -rf /"),
        )
        make_due(store, task.id)

        asyncio.run(run_to_terminal(make_service(store)))

        run = store.list_runs(task.id)[0]
        assert run.status == RUN_UNVERIFIED_STATUS
        assert run.verdict == VERDICT_UNKNOWN
        assert run.verification is not None
        assert run.verification.status is VerificationStatus.REJECTED
        # The reason is quoted, and it is a reason -- not the string "command
        # is allowed", which is what the safety gate puts in that field on the
        # paths that do not refuse.
        assert "验收命令被拒绝" in run.error
        assert "command is allowed" not in run.error
    finally:
        store.close()


def test_a_check_that_cannot_be_started_is_unverified(tmp_path):
    """A missing binary is a fact about the machine, not about the work."""
    store = make_store(tmp_path)
    try:
        task = make_task(
            store,
            workspace_root=str(tmp_path),
            acceptance=Acceptance(
                criteria=["run the validator"],
                verify_command="definitely-not-a-real-binary --check",
            ),
        )
        make_due(store, task.id)

        asyncio.run(run_to_terminal(make_service(store)))

        run = store.list_runs(task.id)[0]
        assert run.status == RUN_UNVERIFIED_STATUS
        assert run.verdict == VERDICT_UNKNOWN
        assert run.verification is not None
        assert run.verification.status is VerificationStatus.SETUP_ERROR
    finally:
        store.close()


@pytest.mark.parametrize(
    "status",
    [
        VerificationStatus.TIMEOUT,
        VerificationStatus.CANCELLED,
        VerificationStatus.REJECTED,
        VerificationStatus.SETUP_ERROR,
    ],
)
def test_only_passed_and_failed_are_verdicts_about_the_work(status):
    """The invariant the whole vocabulary rests on.

    Four of the six verification statuses are statements about the *check*.
    If any of them mapped to ``failed``, a task whose check could not run
    would be retried, would skip its downstream steps, and would be raised
    for attention -- all on the strength of something nobody observed.
    """
    assert verdict_for(status) == VERDICT_UNKNOWN


def test_an_unverified_run_is_retried_and_asks_for_attention(tmp_path):
    """``unverified`` is terminal and worth acting on -- just not a failure."""
    store = make_store(tmp_path)
    try:
        task = make_task(store, workspace_root=str(tmp_path))
        force_acceptance(
            store,
            task.id,
            Acceptance(criteria=["checkable"], verify_command="rm -rf /"),
        )
        make_due(store, task.id)

        asyncio.run(run_to_terminal(make_service(store)))

        run = store.list_runs(task.id)[0]
        assert run.status == RUN_UNVERIFIED_STATUS
        assert run_needs_attention(run) is True
        # Both representations of the same rule, walked through the same run.
        assert store.unacknowledged_attention_counts() == {task.id: 1}
    finally:
        store.close()


# ── 3. A self-report can only lower the verdict ─────────────────────────────


def test_a_self_report_of_failure_fails_the_run_even_when_the_check_passed(tmp_path):
    """The run knows something the check does not.

    A check that passes is evidence that the narrow thing it tests is true,
    not that the job is done.  A run saying "I could not do this" is worth
    acting on, and it must not be overruled by a green tick.
    """
    store = make_store(tmp_path)
    try:
        task = make_task(
            store,
            workspace_root=str(tmp_path),
            acceptance=Acceptance(criteria=["always true"], verify_command="true"),
        )
        make_due(store, task.id)

        asyncio.run(
            run_to_terminal(
                make_service(store, self_report=(VERDICT_FAILED, "the API was down"))
            )
        )

        run = store.list_runs(task.id)[0]
        assert run.status == "failed"
        assert run.verdict == VERDICT_FAILED
        # Both reasons survive, because a run can be wrong in more than one
        # way and "which of these was it" should not have to discard the rest.
        assert "the API was down" in run.error
        # And the check's own result is still recorded as what it was.
        assert run.verification is not None
        assert run.verification.status is VerificationStatus.PASSED
    finally:
        store.close()


def test_a_self_report_cannot_upgrade_an_unknown_verdict():
    """The asymmetry, stated as arithmetic.

    Either source can fail a run; both must pass for it to pass.  There is no
    combination in which an agent's own assurance turns "we could not check"
    into "it passed" -- and no way to say "it passed" at all.
    """
    assert combine_verdicts(VERDICT_UNKNOWN, VERDICT_FAILED) == VERDICT_FAILED
    assert combine_verdicts(VERDICT_UNKNOWN) == VERDICT_UNKNOWN
    assert combine_verdicts(VERDICT_PASSED, VERDICT_FAILED) == VERDICT_FAILED
    assert combine_verdicts(VERDICT_PASSED) == VERDICT_PASSED
    assert combine_verdicts() == VERDICT_NONE
    assert combine_verdicts(VERDICT_NONE, VERDICT_NONE) == VERDICT_NONE


def test_a_self_report_of_failure_with_no_check_at_all_is_failed(tmp_path):
    """Most tasks declare no check, so this is the common way a run reports
    that it could not do its job."""
    store = make_store(tmp_path)
    try:
        task = make_task(store, workspace_root=str(tmp_path))
        make_due(store, task.id)

        asyncio.run(
            run_to_terminal(
                make_service(store, self_report=(VERDICT_FAILED, "no credentials"))
            )
        )

        run = store.list_runs(task.id)[0]
        assert run.status == "failed"
        assert run.verdict == VERDICT_FAILED
        assert "no credentials" in run.error
    finally:
        store.close()


# ── 4. The verdict decides what happens to the chain ────────────────────────


def linear_workflow(acceptance: Acceptance | None = None) -> Workflow:
    return Workflow(
        name="linear",
        steps=[
            WorkflowStep(
                key="collect",
                name="collect",
                kind="agent_prompt",
                payload={"prompt": "collect"},
                trigger=clock(),
                acceptance=acceptance or Acceptance(),
            ),
            WorkflowStep(
                key="analyze",
                name="analyze",
                kind="agent_prompt",
                payload={"prompt": "analyze"},
                depends_on=["collect"],
            ),
            WorkflowStep(
                key="publish",
                name="publish",
                kind="agent_prompt",
                payload={"prompt": "publish"},
                depends_on=["analyze"],
            ),
        ],
    )


def test_a_step_that_fails_its_check_skips_the_steps_below_it(tmp_path):
    """The reason acceptance is load-bearing rather than decorative.

    A dependent step waits for its upstreams to have *succeeded*.  If
    "succeeded" meant only "delivered", a step whose check failed would pass
    its output down the chain, and the failure would surface as a wrong
    result three steps later instead of here.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(
            linear_workflow(Acceptance(criteria=["checkable"], verify_command="false"))
        )
        tasks = store.step_tasks(workflow.id)
        make_due(store, tasks["collect"].id)

        asyncio.run(run_to_terminal(make_service(store)))

        collect_runs = store.list_runs(tasks["collect"].id)
        assert [run.status for run in collect_runs] == ["failed"]
        assert collect_runs[0].verdict == VERDICT_FAILED
        # Everything below it, and in silence: a step waiting for a success
        # that is never coming would look like a step nobody got around to.
        assert [run.status for run in store.list_runs(tasks["analyze"].id)] == ["skipped"]
        assert [run.status for run in store.list_runs(tasks["publish"].id)] == ["skipped"]
    finally:
        store.close()


def test_an_unverified_step_also_stops_the_chain(tmp_path):
    """The judgement is "did it succeed", and "we could not tell" is not yes.

    Letting the chain continue on an unknown verdict would be a guess made on
    the user's behalf, and the guess would be invisible.
    """
    store = make_store(tmp_path)
    try:
        workflow = store.create_workflow(linear_workflow())
        tasks = store.step_tasks(workflow.id)
        force_acceptance(
            store,
            tasks["collect"].id,
            Acceptance(criteria=["checkable"], verify_command="rm -rf /"),
        )
        make_due(store, tasks["collect"].id)

        asyncio.run(run_to_terminal(make_service(store)))

        collect_runs = store.list_runs(tasks["collect"].id)
        assert [run.status for run in collect_runs] == [RUN_UNVERIFIED_STATUS]
        assert [run.status for run in store.list_runs(tasks["analyze"].id)] == ["skipped"]
    finally:
        store.close()


# ── 5. Creation refuses what could never be judged ──────────────────────────


def test_a_high_risk_command_is_refused_where_the_task_is_written(tmp_path):
    """Refused while somebody is looking at it, not discovered at 3am.

    A task whose criterion could never be evaluated produces a run with no
    verdict at all, which is strictly worse than a refusal the author can act
    on.
    """
    store = make_store(tmp_path)
    try:
        with pytest.raises(ValueError) as caught:
            make_task(
                store,
                workspace_root=str(tmp_path),
                acceptance=Acceptance(
                    criteria=["clean up"], verify_command="rm -rf /"
                ),
            )
        message = str(caught.value)
        assert "验收命令被拒绝" in message
        # And it says why, in terms the author can act on.
        assert "风险等级" in message
        # Nothing was written.
        assert store.list_tasks() == []
    finally:
        store.close()


def test_a_write_profile_criterion_is_checked_against_the_task_s_own_folder(tmp_path):
    """Root-sensitivity, which is why validation and materialization share one
    answer to "where does this run".

    ``allowed_roots`` only *exempts* absolute paths inside them; it never
    refuses one.  So the folder a criterion is checked against has to be the
    folder the run will actually use, or a perfectly good command is refused
    for a reason that has nothing to do with the command.
    """
    workspace = tmp_path / "project"
    workspace.mkdir()
    store = make_store(tmp_path)
    try:
        task = make_task(
            store,
            workspace_root=str(workspace),
            acceptance=Acceptance(
                criteria=["the artifact exists"],
                verify_command=f"test -f {workspace}/artifact.txt",
            ),
        )
        assert task.acceptance.verify_command.endswith("artifact.txt")
    finally:
        store.close()


def test_a_criterion_that_is_too_long_is_refused(tmp_path):
    store = make_store(tmp_path)
    try:
        with pytest.raises(ValueError):
            make_task(
                store,
                workspace_root=str(tmp_path),
                acceptance=Acceptance(criteria=["x" * 600]),
            )
    finally:
        store.close()


# ── 6. Two tasks that disagree about "done" are two tasks ───────────────────


def test_the_criterion_is_part_of_a_task_s_identity(tmp_path):
    """Otherwise "make this run only when the tests pass" is answered with the
    existing task that runs regardless -- and reported as done."""
    store = make_store(tmp_path)
    try:
        plain = make_task(store, name="same", workspace_root=str(tmp_path))
        again = store.find_matching_task(
            NewScheduledTask(
                name="same",
                kind="agent_prompt",
                trigger=clock(),
                payload={"prompt": "do the thing"},
                delivery_mode="standalone",
                delivery_target=DeliveryTarget.standalone(),
                workspace_root=str(tmp_path),
                acceptance=Acceptance(),
            )
        )
        stricter = store.find_matching_task(
            NewScheduledTask(
                name="same",
                kind="agent_prompt",
                trigger=clock(),
                payload={"prompt": "do the thing"},
                delivery_mode="standalone",
                delivery_target=DeliveryTarget.standalone(),
                workspace_root=str(tmp_path),
                acceptance=Acceptance(criteria=["the tests pass"]),
            )
        )

        assert again is not None and again.id == plain.id
        assert stricter is None
    finally:
        store.close()


# ── 7. What a run reports, and what it cannot ───────────────────────────────


def test_report_outcome_refuses_outside_a_scheduled_run():
    """In a conversation there is a person to tell, so writing a verdict to
    nowhere would only look as though something had been done."""
    from agent import BuiltinTools, MemoryPalace, ToolRegistry

    registry = ToolRegistry()
    memory = MemoryPalace(base_dir=Path("/tmp") / f"probe-{uuid4().hex}", context_dir=None)
    tools = BuiltinTools(memory=memory, registry=registry)

    with pytest.raises(ValueError) as caught:
        tools._report_outcome("could not finish")
    assert "只能在定时任务的运行中使用" in str(caught.value)


def test_report_outcome_records_only_a_failure(tmp_path):
    """There is deliberately no way to report success, so the only verdict a
    self-report can carry is ``failed``."""
    from agent import BuiltinTools, MemoryPalace, ToolRegistry
    from agent.tools.runtime import RunSelfReport, _active_run_self_report

    registry = ToolRegistry()
    memory = MemoryPalace(base_dir=tmp_path / "memory", context_dir=tmp_path / "ctx")
    tools = BuiltinTools(memory=memory, registry=registry)

    report = RunSelfReport()
    token = _active_run_self_report.set(report)
    try:
        tools._report_outcome("the source was empty")
    finally:
        _active_run_self_report.reset(token)

    assert report.verdict == VERDICT_FAILED
    assert report.reason == "the source was empty"


def test_report_outcome_requires_a_reason():
    from agent import BuiltinTools, MemoryPalace, ToolRegistry
    from agent.tools.runtime import RunSelfReport, _active_run_self_report

    registry = ToolRegistry()
    memory = MemoryPalace(
        base_dir=Path("/tmp") / f"probe-{uuid4().hex}", context_dir=None
    )
    tools = BuiltinTools(memory=memory, registry=registry)

    report = RunSelfReport()
    token = _active_run_self_report.set(report)
    try:
        with pytest.raises(ValueError):
            tools._report_outcome("   ")
    finally:
        _active_run_self_report.reset(token)

    assert report.verdict == VERDICT_NONE


# ── 8. What the interface is told ───────────────────────────────────────────


def test_the_run_payload_carries_the_verdict_beside_the_status(tmp_path):
    """The interface cannot show the two side by side unless both arrive.

    A run's status and its verdict are different answers, and the whole point
    of separating them is lost if the API sends only one.
    """
    from agent.channels.web import _scheduler_run_payload, _scheduler_task_payload

    store = make_store(tmp_path)
    try:
        task = make_task(
            store,
            workspace_root=str(tmp_path),
            acceptance=Acceptance(criteria=["the tests pass"], verify_command="true"),
        )
        make_due(store, task.id)
        asyncio.run(run_to_terminal(make_service(store)))
        run = store.list_runs(task.id)[0]

        payload = _scheduler_run_payload(run, with_snapshot=False)
        assert payload["status"] == "succeeded"
        assert payload["verdict"] == VERDICT_PASSED
        assert payload["verification"]["status"] == "passed"

        task_payload = _scheduler_task_payload(task)
        assert task_payload["acceptance"] == {
            "criteria": ["the tests pass"],
            "verify_command": "true",
        }
    finally:
        store.close()


def test_a_task_with_no_criterion_still_reports_an_acceptance_object(tmp_path):
    """Always an object, never absent: the client reads the fields without
    checking whether the key exists, and "nothing was declared" is a value."""
    from agent.channels.web import _scheduler_task_payload

    store = make_store(tmp_path)
    try:
        task = make_task(store, workspace_root=str(tmp_path))
        payload = _scheduler_task_payload(task)
        assert payload["acceptance"] == {"criteria": [], "verify_command": ""}
    finally:
        store.close()


def test_the_verdict_survives_reopening_the_database(tmp_path):
    """It is stored, not derived at read time: the verification record is the
    evidence, and evidence that only exists in memory is not evidence."""
    store = make_store(tmp_path)
    db_path = store.db_path
    try:
        task = make_task(
            store,
            workspace_root=str(tmp_path),
            acceptance=Acceptance(criteria=["checkable"], verify_command="false"),
        )
        make_due(store, task.id)
        asyncio.run(run_to_terminal(make_service(store)))
        run_id = store.list_runs(task.id)[0].id
    finally:
        store.close()

    reopened = SchedulerStore(db_path=db_path)
    try:
        run = reopened.get_run(task.id, run_id)
        assert run.status == "failed"
        assert run.verdict == VERDICT_FAILED
        assert run.verification is not None
        assert run.verification.exit_code == 1
        # And the criterion the run was judged by, read back from the task.
        assert reopened.get_task(task.id).acceptance.verify_command == "false"
    finally:
        reopened.close()


def test_an_old_database_is_upgraded_without_inventing_verdicts(tmp_path):
    """An empty verdict means "nothing was declared", which is also true of
    every run recorded before this existed -- so the migration backfills
    nothing and rewrites no history."""
    store = make_store(tmp_path)
    try:
        task = make_task(store, workspace_root=str(tmp_path))
        make_due(store, task.id)
        asyncio.run(run_to_terminal(make_service(store)))
        run = store.list_runs(task.id)[0]
        assert run.verdict == VERDICT_NONE
        assert run.verification is None
        assert task.acceptance == Acceptance()
    finally:
        store.close()

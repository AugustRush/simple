"""Translating between the scheduler's rows and its domain objects -- and validating them.

Extracted from ``SchedulerStore`` (see the remaining-big-files survey): the row mappers, the
step/identity spec builders, and the two validators a task must pass to be created at all.
None of them touch the store's state; they take what they translate or check.

Three members of the measured cluster are **deliberately not here**: ``workflow_blocked_steps``
carries the store's own ``@_synchronized`` decoration -- locking is the store's concern --
``_live_workflow_step`` calls ``self.get_workflow``, and ``_join_arrivals`` reads ids the caller
holds.  The survey counted all three as ``self``-free; only the first half of that was true,
which is what reading the moved code against its class is for.

Imported back into ``agent/scheduler/store.py``, so its call sites keep their names.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from agent import shared
from agent.scheduler.models import (
    ATTENTION_STATUSES,
    Acceptance,
    DeliveryTarget,
    NewScheduledTask,
    SignalEmission,
    Workflow,
    declared_products,
    products_payload,
    ScheduledTask,
    TaskRun,
    TriggerSpec,
    UTC,
    WorkflowStep,
    decode_verification,
    normalize_products,
    optional_datetime,
    validate_acceptance,
    validate_products,
    validate_workflow_graph,
    workflow_step_order,
)

__all__ = ['_task_from_row', '_run_from_row', '_check_acceptance', '_check_products', '_identity_values', '_attention_clause', '_step_workspaces', '_step_task_spec']


def _task_from_row(row: sqlite3.Row) -> ScheduledTask:
    return ScheduledTask(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        enabled=bool(row["enabled"]),
        trigger=TriggerSpec.from_json(row["trigger_json"]),
        payload=json.loads(row["payload_json"]),
        delivery_mode=row["delivery_mode"],
        delivery_target=DeliveryTarget.from_json(row["delivery_target_json"]),
        model_override=row["model_override"],
        workspace_root=row["workspace_root"],
        context_policy=row["context_policy"],
        timeout_seconds=int(row["timeout_seconds"]),
        retry_policy=json.loads(row["retry_policy_json"]),
        selected_skills=json.loads(row["selected_skills_json"]),
        permission_profile=row["permission_profile"],
        next_run_at=optional_datetime(row["next_run_at"]),
        lease_until=optional_datetime(row["lease_until"]),
        active_run_id=row["active_run_id"],
        last_run_at=optional_datetime(row["last_run_at"]),
        last_success_at=optional_datetime(row["last_success_at"]),
        created_at=optional_datetime(row["created_at"]) or datetime.now(UTC),
        updated_at=optional_datetime(row["updated_at"]) or datetime.now(UTC),
        acceptance=Acceptance.from_json(row["acceptance_json"]),
        produces=normalize_products(json.loads(row["produces_json"] or "[]")),
        workflow_id=row["workflow_id"] or "",
        step_key=row["step_key"] or "",
        session_id=row["session_id"] or "",
        request_quote=row["request_quote"] or "",
    )


def _run_from_row(row: sqlite3.Row) -> TaskRun:
    return TaskRun(
        id=row["id"],
        task_id=row["task_id"],
        scheduled_for=optional_datetime(row["scheduled_for"]) or datetime.now(UTC),
        started_at=optional_datetime(row["started_at"]) or datetime.now(UTC),
        finished_at=optional_datetime(row["finished_at"]),
        status=row["status"],
        summary=row["summary"],
        error=row["error"],
        output_path=row["output_path"],
        delivery_status=row["delivery_status"],
        verdict=row["verdict"] or "",
        verification=decode_verification(row["verification_json"]),
        products=products_payload(json.loads(row["products_json"] or "[]")),
        config_snapshot=json.loads(row["config_snapshot_json"]),
        trigger_source=row["trigger_source"],
        attempt=int(row["attempt"]),
        cancel_requested_at=optional_datetime(row["cancel_requested_at"]),
        retry_of_run_id=row["retry_of_run_id"],
        workflow_run_id=row["workflow_run_id"],
        acknowledged_at=optional_datetime(row["acknowledged_at"]),
        missed_count=int(row["missed_count"] or 0),
        created_at=optional_datetime(row["created_at"]),
        updated_at=optional_datetime(row["updated_at"]),
    )


def _check_acceptance(task: NewScheduledTask) -> None:
    """Refuse an acceptance criterion this task could never be judged by.

    Called on every write of a task definition, which is the only moment
    the check can be refused while somebody is still looking at the screen.
    The workspace is the task's own; the output root is the same one a run's
    verifier will be given, so a command that passes here is not rejected
    later for being pointed at the wrong directory.
    """
    validate_acceptance(
        getattr(task, "acceptance", None),
        workspace_root=str(getattr(task, "workspace_root", "") or ""),
        output_dir=str(shared.DEFAULT_OUTPUT_DIR),
        label=f"任务「{getattr(task, 'name', '')}」",
    )


def _check_products(task: NewScheduledTask) -> None:
    """Refuse a product this task could never be held to.

    Same moment and same argument as :meth:`_check_acceptance`: a
    declaration whose only failure mode is a run at 3am that quietly
    produces nothing is worth refusing while somebody is watching.

    The check is on the *shape* of the paths.  Whether the task can
    actually write there is a question for its permission profile, which
    refuses at run time -- and answering it here as well would be a second
    opinion about the same thing, the one that drifts.
    """
    validate_products(
        getattr(task, "produces", None),
        label=f"任务「{getattr(task, 'name', '')}」",
    )


def _identity_values(task: NewScheduledTask) -> tuple[Any, ...]:
    """*task*'s identity columns, in ``_TASK_IDENTITY_COLUMNS`` order.

    Positional against the column list on purpose: a named mapping would
    let a new column be added to one half and silently default to nothing
    in the other, which is how the two lists this replaces drifted apart.
    """
    return (
        task.name,
        task.kind,
        task.trigger.to_json(),
        json.dumps(task.payload, ensure_ascii=False),
        task.delivery_mode,
        task.delivery_target.to_json(),
        task.model_override,
        task.workspace_root,
        task.context_policy,
        int(task.timeout_seconds),
        json.dumps(task.retry_policy, ensure_ascii=False),
        json.dumps(task.selected_skills, ensure_ascii=False),
        task.permission_profile,
        # Part of the identity, not a decoration on it.  Two tasks that
        # disagree about what "done" means are two different tasks, and
        # treating them as one would answer "make this run only when the
        # tests pass" with the existing task that runs regardless --
        # reporting success while the criterion the caller just stated is
        # silently discarded.
        task.acceptance.to_json(),
        # Part of the identity for the same reason the criterion above is:
        # two tasks that disagree about what they leave behind are two
        # different tasks, and folding them together would answer "also
        # write this report" with the task that writes the other one --
        # while reporting success.
        json.dumps(declared_products(task), ensure_ascii=False),
        task.workflow_id,
        task.step_key,
    )


def _attention_clause() -> tuple[str, list[Any]]:
    """Runs a person should look at.

    Two ways in.  The run ended badly, or it arrived carrying the news
    that earlier occurrences were skipped while nothing was running.  The
    second is not a failure of *this* run -- it succeeded -- but it is the
    one case where waiting to be read does not work: the run that resumes
    the schedule reports success, so nothing else in the system will ever
    mention the gap, and a daily report can be missing for a week with
    every run marked fine.

    The status list is read from :data:`ATTENTION_STATUSES` rather than
    written out here, which is what keeps this query and
    :func:`run_needs_attention` -- the same rule in Python -- from
    disagreeing when a status is added.  ``unverified`` arrived that way.
    """
    placeholders = ", ".join("?" for _ in ATTENTION_STATUSES)
    return (
        f"(status IN ({placeholders}) OR missed_count > 0)",
        list(ATTENTION_STATUSES),
    )


def _step_workspaces(steps: list[WorkflowStep], order: list[str]) -> tuple[dict[str, str], list[str]]:
    """Where each step will run, resolved, and which ones inherited it.

    One answer, decided in one place, because two callers need it and they
    must agree: materialization writes the folder onto each task, and graph
    validation checks each step's acceptance criterion against the folder
    the step will actually run in.  The shell safety gate exempts absolute
    paths only inside the roots it is handed, so a criterion checked against
    the wrong folder is refused for a reason that has nothing to do with the
    command -- which is why "where does this run" cannot have two answers.
    """
    by_key = {str(step.key).strip(): step for step in steps}
    # A step with no folder of its own runs where the workflow's entry
    # steps run.  A chain is one job, and steps that silently split across
    # directories would write half a result to each.
    inherited = ""
    for key in order:
        step = by_key.get(key)
        if (
            step is not None
            and step.is_entry()
            and str(step.workspace_root or "").strip()
        ):
            inherited = str(
                Path(step.workspace_root).expanduser().resolve(strict=False)
            )
            break
    resolved: dict[str, str] = {}
    took_inherited: list[str] = []
    for key in order:
        step = by_key.get(key)
        if step is None:
            continue
        own = str(step.workspace_root or "").strip()
        if not own and inherited:
            took_inherited.append(key)
            resolved[key] = inherited
            continue
        resolved[key] = (
            str(Path(own).expanduser().resolve(strict=False))
            if own
            else str(Path.cwd().resolve())
        )
    return resolved, took_inherited


def _step_task_spec(step: WorkflowStep, trigger: TriggerSpec, *, workflow_id: str, enabled: bool, workspace: str, request_quote: str = "", ) -> NewScheduledTask:
    target = step.delivery_target
    if target is None:
        target = DeliveryTarget.standalone()
    return NewScheduledTask(
        name=step.name,
        kind=step.kind,
        trigger=trigger,
        payload=dict(step.payload),
        delivery_mode=step.delivery_mode,
        delivery_target=target,
        model_override=step.model_override,
        timeout_seconds=int(step.timeout_seconds),
        selected_skills=list(step.selected_skills),
        # Already resolved by ``_step_workspaces``, which is the only place
        # that decides where a step runs.  Resolving again here would be a
        # second answer to the same question.
        workspace_root=workspace,
        permission_profile=step.permission_profile,
        context_policy=step.context_policy,
        # Carried through so the step's own task is judged by the criterion
        # written on the graph.  A step's acceptance is what decides whether
        # the steps below it run, so losing it here would turn a chained
        # workflow back into a sequence of unrelated tasks.
        acceptance=step.acceptance,
        # And what it promises to leave behind, carried through for the
        # same reason: the graph's edges say when the next step runs, and
        # these say what it can count on being there when it does.  A step
        # that lost this here would be a step whose promise reached the
        # graph and never reached the run that has to keep it.
        produces=list(step.produces),
        # Carried through for the same reason, and it is the whole point of
        # the field: `_enqueue_automatic_retry` reads this off the run's
        # snapshot, so a step whose policy stopped here would be built with
        # the no-retry default no matter what the graph said.  One flaky
        # step in the middle of a chain would then skip every step below it.
        retry_policy=dict(step.retry_policy),
        enabled=enabled,
        workflow_id=workflow_id,
        step_key=step.key,
        # Nobody asked for this step by name -- the chain was asked for,
        # and the step is how the chain runs.  So the step carries the
        # chain's words rather than none: a step row that says "asked for
        # by nobody" is indistinguishable from a task that appeared without
        # being asked for, which is the one thing this must not look like.
        request_quote=request_quote,
    )


def _emission_from_row(
    row: sqlite3.Row,
) -> SignalEmission:
    return SignalEmission(
        id=row["id"],
        name=row["name"],
        payload=json.loads(row["payload_json"] or "{}"),
        source=row["source"],
        depth=int(row["depth"]),
        origin_id=row["origin_id"],
        state=row["state"],
        reason=row["reason"],
        delivered_run_id=row["delivered_run_id"],
        created_at=optional_datetime(row["created_at"]),
        delivered_at=optional_datetime(row["delivered_at"]),
    )


@staticmethod
def _emission_workflow_run_id(emission: SignalEmission) -> str:
    return str(emission.payload.get("workflow_run_id", "") or "").strip()


def _workflow_from_row(
    row: sqlite3.Row,
) -> Workflow:
    return Workflow.from_graph(
        row["graph_json"],
        workflow_id=row["id"],
        request_quote=row["request_quote"] or "",
        created_at=optional_datetime(row["created_at"]),
        updated_at=optional_datetime(row["updated_at"]),
        version=max(1, int(row["version"] or 1)),
    )


def _validate_graph(
    steps: list[WorkflowStep],
) -> list[str]:
    """Check a graph, including every step's acceptance criterion.

    The folders are resolved first so each criterion is checked against the
    directory its step will actually run in.  Nothing is written before this
    returns, which is what keeps a refused graph from leaving a workflow row
    with no tasks behind it -- the state ``create_workflow`` exists to make
    impossible.
    """
    order = workflow_step_order(steps)
    workspaces, _ = _step_workspaces(steps, order)
    return validate_workflow_graph(
        steps,
        acceptance_workspaces=workspaces,
        output_dir=str(shared.DEFAULT_OUTPUT_DIR),
    )

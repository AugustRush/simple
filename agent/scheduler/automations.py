"""One list for what a person manages in 自动化: plain tasks and chains.

Why this module exists
----------------------
A chain ("workflow") is not a second kind of thing in this scheduler, and the code says so
in three places: ``WorkflowStep`` is documented as *"one step of a workflow: a task, plus
what has to finish before it runs"*; a chain run is anchored to a task run
(``workflow_runs.root_task_id``); and the task payload already carries ``workflow_id`` /
``step_key``. ``tests/test_scheduler_workflows.py`` states the same premise at the top:
**"A step is a task that already exists."**

What was still split in two was the *presentation*: two list endpoints, two tool families
(``schedule_*`` / ``workflow_*``) and two page tabs, for one mechanism. That split is what
makes an operator ask "should this be a task or a workflow?" when the honest answer is
"the same thing, with one step or several".

So this module does not merge two models. It stops presenting one model as two, using the
data the store already has.

What is listed, and what is not
-------------------------------
Only what a person manages: **standalone tasks** (a task that is not a step of a chain)
and **chains** (one row each). A step task is *inside* its chain row and is not listed
again -- listing it twice is the double-count this endpoint exists to prevent, the same
invariant the attention snapshot's "add up the rows and check them against the number"
invitation is about.

Read-only on purpose
--------------------
Nothing here writes. Writing is still two paths (``schedule_*``, ``workflow_*``) until the
tool surface is unified; keeping the read model free of writes is what lets it be shipped,
tested and used by the page before any of that changes.
"""

from __future__ import annotations

from typing import Any, Optional

from .models import ScheduledTask, Workflow, WorkflowStep, _iso

__all__ = ["automation_rows", "AUTOMATION_KIND_TASK", "AUTOMATION_KIND_CHAIN"]

AUTOMATION_KIND_TASK = "task"
AUTOMATION_KIND_CHAIN = "chain"


def _entry_step(workflow: Workflow) -> Optional[WorkflowStep]:
    """The step that starts a chain: no upstreams, and a trigger of its own.

    An entry step is an ordinary scheduled task -- that is the whole reason a chain can
    start from a clock, a signal or anything else the scheduler understands.  A chain
    whose entry step has no trigger is broken by construction (``validate_workflow_graph``
    refuses it), but reading is not the place to raise: the row still has to render.
    """
    for step in workflow.steps:
        if not step.depends_on:
            return step
    return None


def _later(a: Optional[Any], b: Optional[Any]) -> Optional[Any]:
    """The later of two datetimes, tolerating ``None``."""
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


def _earlier(a: Optional[Any], b: Optional[Any]) -> Optional[Any]:
    """The earlier of two datetimes, tolerating ``None``."""
    if a is None:
        return b
    if b is None:
        return a
    return a if a <= b else b


def _task_row(
    *,
    task: ScheduledTask,
    latest_run: Any,
    attention: int,
) -> dict[str, Any]:
    """A standalone task, in the shared shape: a chain of one step."""
    return {
        "id": task.id,
        "kind": AUTOMATION_KIND_TASK,
        "name": task.name,
        # The two ids the existing detail/run/cancel endpoints are keyed by.  A caller
        # that wants "the thing this row is about" uses `id`; a caller that has to call
        # an existing endpoint uses whichever of these is set -- which is the seam that
        # lets the page be unified without rewriting a single run-history route.
        "task_id": task.id,
        "workflow_id": "",
        "enabled": bool(task.enabled),
        "step_count": 1,
        "trigger_type": task.trigger.trigger_type,
        "trigger": dict(task.trigger.payload or {}),
        "next_run_at": _iso(task.next_run_at) if task.next_run_at else None,
        "last_run_at": _iso(task.last_run_at) if task.last_run_at else None,
        "last_status": getattr(latest_run, "status", "") or "",
        "attention": int(attention),
        "delivery_mode": task.delivery_mode,
        "permission_profile": task.permission_profile,
    }


def _chain_row(
    *,
    workflow: Workflow,
    step_tasks: dict[str, ScheduledTask],
    latest_runs: dict[str, Any],
    attention: int,
) -> dict[str, Any]:
    """A chain, in the shared shape: one row for N steps.

    The clock a chain is judged by is its **entry** step's: a chain with a daily entry does
    not run daily -- its steps run as their upstreams finish -- but "when does this next
    start" is the entry step's answer, and that is the question the list column asks.
    """
    entry = _entry_step(workflow)
    entry_task = step_tasks.get(str(entry.key).strip()) if entry is not None else None

    next_run: Any = None
    last_run: Any = None
    last_status = ""
    for task in step_tasks.values():
        next_run = _earlier(next_run, task.next_run_at)
        run = latest_runs.get(task.id)
        if run is None:
            continue
        finished = getattr(run, "finished_at", None) or getattr(run, "started_at", None)
        if last_run is None or (finished is not None and finished >= last_run):
            last_run = finished
            last_status = getattr(run, "status", "") or ""

    return {
        "id": workflow.id,
        "kind": AUTOMATION_KIND_CHAIN,
        "name": workflow.name,
        "task_id": entry_task.id if entry_task is not None else "",
        "workflow_id": workflow.id,
        "enabled": bool(workflow.enabled),
        "step_count": len(workflow.steps),
        "trigger_type": (
            entry.trigger.trigger_type
            if entry is not None and entry.trigger is not None
            else "signal"
        ),
        "trigger": (
            dict(entry.trigger.payload or {})
            if entry is not None and entry.trigger is not None
            else {}
        ),
        "next_run_at": _iso(next_run) if next_run else None,
        "last_run_at": _iso(last_run) if last_run else None,
        "last_status": last_status,
        "attention": int(attention),
        "description": workflow.description,
    }


def own_errand_task_ids(store: Any) -> set[str]:
    """Ids of tasks the patrol created to carry out one of its own proposals.

    They are machinery, not automations: the thing a person reads is the
    proposal's outcome (on 自主), and the errand exists only long enough to run
    once. Listing them here put the agent's errands and the tasks a person set up
    in one list -- measured, five of eight rows on a fresh install were one-off
    errands the patrol had already finished.

    ``agent.autonomy.is_own_errand`` is the one definition, read rather than
    copied: a second spelling of "this is the agent's own" is how the list and
    the patrol would drift apart.
    """
    from agent.autonomy import is_own_errand

    return {
        task.id
        for task in store.list_tasks()
        if is_own_errand(getattr(task, "payload", None))
    }


def automation_rows(store: Any, snapshot: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """Every automation a person manages: standalone tasks, then chains.

    One ``attention_snapshot()`` for the whole list, for the reason that snapshot's own
    docstring gives: a count read separately from the rows it counts can be a number the
    page cannot reproduce.

    Ordered as a person reads a list: chains first by creation, then standalone tasks by
    creation -- chains are the bigger thing and their rows explain more. Sorted here
    rather than in SQL because both halves come from different tables.

    The patrol's own errands are left out (see :func:`own_errand_task_ids`): they are
    one-off runs the agent started for itself, whose result is on the proposal that
    started them.
    """
    # The caller may already hold a snapshot for the same response (the badge number is
    # read from one too); reusing it is what keeps "add up the rows, get the number" true.
    if snapshot is None:
        snapshot = store.attention_snapshot()
    unseen: dict[str, int] = snapshot.get("counts") or {}
    hidden = own_errand_task_ids(store)

    rows: list[dict[str, Any]] = []
    chained: set[str] = set()

    for workflow in store.list_workflows():
        step_tasks = store.step_tasks(workflow.id)
        chained.update(task.id for task in step_tasks.values())
        latest_runs = {
            task.id: store.latest_run(task.id) for task in step_tasks.values()
        }
        rows.append(
            _chain_row(
                workflow=workflow,
                step_tasks=step_tasks,
                latest_runs=latest_runs,
                attention=sum(int(unseen.get(task.id, 0)) for task in step_tasks.values()),
            )
        )

    for task in store.list_tasks():
        if task.id in chained:
            # A step of a chain. It belongs to its chain's row; a second row here is the
            # same thing counted twice, which is what this endpoint must never do.
            continue
        if task.id in hidden:
            # The patrol's own one-off errand. Its result is on the proposal.
            continue
        rows.append(
            _task_row(
                task=task,
                latest_run=store.latest_run(task.id),
                attention=int(unseen.get(task.id, 0)),
            )
        )

    return rows

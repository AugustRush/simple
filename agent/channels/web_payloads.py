"""The JSON shape the interface reads for scheduled work.

Why this module exists
----------------------
Every fact the web interface can see about scheduled work is built by these
eight functions: one task, one run, one workflow, and the attention snapshot
behind the badge.  They sat at the top of ``agent/channels/web.py``, where
4191 lines of HTTP handlers made the payload shape hard to read as one thing.

The shape is a contract with a client that cannot see the store, so the
reasoning belongs next to the field rather than next to the handler that
happens to return it.  Three of those decisions are load-bearing, and the
comments carrying them moved here word for word:

- **``status`` and ``verdict`` are two questions, not one.**  ``status`` says
  whether the run happened; ``verdict`` says whether what it produced met the
  bar the task was given.  Collapsing them is what once made a useless answer
  read as success.
- **``with_snapshot`` is off for the copies embedded in a list.**  A run's
  config snapshot is the largest field it has, it is read only by the
  run-detail drawer, and that drawer is fed by the run-history endpoint rather
  than by the list.
- **``in_flight`` is answered here, not derived by the client.**  A client uses
  it to decide how soon to ask again; a signal-woken run is written down as
  ``queued`` with no ``active_run_id`` until a later tick claims it, so a
  client re-deriving it from ``active_run_id`` gets exactly that case wrong.

Nothing here reads a request, a component or a module global.  The two
attention builders take a store and call read methods on it; they never open
one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

from agent.scheduler.models import (
    RUN_IN_FLIGHT_STATUSES,
    acceptance_payload,
    run_needs_attention,
)
from agent.verification import verification_payload


def task_in_flight(task: Any, latest_run: Any = None) -> bool:
    """Whether this task has a run that is going to happen, or is happening.

    Answered here rather than left to the client because the client uses it to
    decide how often to ask again, and a client that re-derives it from
    ``active_run_id`` gets it wrong for a signal-woken run: that run is written
    down as ``queued`` and has no ``active_run_id`` until a later tick claims
    it.  One definition, one place to be wrong.
    """
    if getattr(task, "active_run_id", None):
        return True
    return str(getattr(latest_run, "status", "") or "") in RUN_IN_FLIGHT_STATUSES


def scheduler_run_payload(run: Any, *, with_snapshot: bool = True) -> dict[str, Any]:
    """One run, as the interface reads it.

    *with_snapshot* is off for the copies embedded in a list: the run's config
    snapshot is the largest field it has, it is read only by the run-detail
    drawer, and that drawer is fed by the run-history endpoint rather than by
    the list.  Sending it with every task on every poll is the difference
    between a list payload that is mostly answer and one that is mostly a
    field nobody looks at -- and it is the part that grows, because a step's
    snapshot now carries its upstreams' report previews.
    """
    output_path = str(getattr(run, "output_path", "") or "").strip()
    output_available = False
    if output_path:
        try:
            output_available = Path(output_path).expanduser().is_file()
        except OSError:
            pass
    started_at = getattr(run, "started_at", None)
    finished_at = getattr(run, "finished_at", None)
    duration_ms = None
    if started_at is not None and finished_at is not None:
        duration_ms = max(0, round((finished_at - started_at).total_seconds() * 1000))
    payload = {
        "id": str(getattr(run, "id", "") or ""),
        "task_id": str(getattr(run, "task_id", "") or ""),
        "status": str(getattr(run, "status", "") or ""),
        "scheduled_for": (
            run.scheduled_for.isoformat() if getattr(run, "scheduled_for", None) else None
        ),
        "started_at": started_at.isoformat() if started_at else None,
        "finished_at": finished_at.isoformat() if finished_at else None,
        "duration_ms": duration_ms,
        "summary": str(getattr(run, "summary", "") or ""),
        "error": str(getattr(run, "error", "") or ""),
        "delivery_status": str(getattr(run, "delivery_status", "") or ""),
        # Two answers that are not the same question: ``status`` says whether
        # the run happened, ``verdict`` says whether what it produced met the
        # bar the task was given.  They travel separately because a run can
        # deliver a result that does not do the job, and a result that does the
        # job can fail to arrive -- and collapsing them is what made a useless
        # answer read as success.
        "verdict": str(getattr(run, "verdict", "") or ""),
        "verification": verification_payload(getattr(run, "verification", None)),
        "trigger_source": str(getattr(run, "trigger_source", "schedule") or "schedule"),
        "attempt": int(getattr(run, "attempt", 1) or 1),
        "cancel_requested_at": (
            run.cancel_requested_at.isoformat()
            if getattr(run, "cancel_requested_at", None)
            else None
        ),
        "retry_of_run_id": str(getattr(run, "retry_of_run_id", "") or ""),
        "missed_count": int(getattr(run, "missed_count", 0) or 0),
        "acknowledged_at": (
            run.acknowledged_at.isoformat()
            if getattr(run, "acknowledged_at", None)
            else None
        ),
        "needs_attention": run_needs_attention(run),
        "output_available": output_available,
        "output_url": (
            scheduler_output_url(
                str(getattr(run, "task_id", "") or ""),
                str(getattr(run, "id", "") or ""),
                output_path,
            )
            if output_available
            else ""
        ),
    }
    if with_snapshot:
        payload["config_snapshot"] = dict(getattr(run, "config_snapshot", {}) or {})
    return payload


def scheduler_output_url(task_id: str, run_id: str, output_path: str) -> str:
    """Build the ``/api/files`` link for a scheduled run's stored output.

    The link names the run it belongs to so ``GET /api/files`` can verify the
    entitlement against the scheduler store instead of trusting the path.
    """
    return (
        "/api/files?path="
        + quote(str(output_path), safe="")
        + "&task_id="
        + quote(str(task_id), safe="")
        + "&run_id="
        + quote(str(run_id), safe="")
    )


def scheduler_task_payload(
    task: Any, latest_run: Any = None, unseen_attention: int = 0
) -> dict[str, Any]:
    return {
        "id": task.id,
        "name": task.name,
        "kind": task.kind,
        "enabled": task.enabled,
        "trigger_type": task.trigger.trigger_type,
        "trigger": task.trigger.payload,
        "payload": task.payload,
        "delivery_mode": task.delivery_mode,
        "delivery_target": {
            "target_type": task.delivery_target.target_type,
            "payload": task.delivery_target.payload,
        },
        "model_override": task.model_override,
        "workspace_root": task.workspace_root,
        "context_policy": task.context_policy,
        "timeout_seconds": task.timeout_seconds,
        "retry_policy": task.retry_policy,
        "selected_skills": task.selected_skills,
        "permission_profile": task.permission_profile,
        # What this task's runs are judged by.  Sent so the interface can say
        # it, because a criterion the person never saw is indistinguishable
        # from a run that failed for no reason.
        "acceptance": acceptance_payload(getattr(task, "acceptance", None)),
        # Empty for a standalone task, which most are.  Carried on the task
        # rather than looked up from the graph so a task row in the list can
        # say where it belongs without the client holding the whole graph.
        "workflow_id": str(getattr(task, "workflow_id", "") or ""),
        "step_key": str(getattr(task, "step_key", "") or ""),
        # The words that asked for this task.  Sent for the same reason the
        # acceptance criterion above is: a task that appeared without anyone
        # asking for it is what this field exists to make impossible to hide,
        # and that only works if the answer is visible rather than only stored.
        "request_quote": str(getattr(task, "request_quote", "") or ""),
        "unseen_attention": int(unseen_attention or 0),
        "active_run_id": task.active_run_id,
        # The client uses this to choose how soon to ask again, so it travels
        # as an answer rather than as the two fields it is derived from.
        "in_flight": task_in_flight(task, latest_run),
        "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
        "last_run_at": task.last_run_at.isoformat() if task.last_run_at else None,
        "last_success_at": (
            task.last_success_at.isoformat() if task.last_success_at else None
        ),
        "latest_run": (
            scheduler_run_payload(latest_run, with_snapshot=False)
            if latest_run
            else None
        ),
    }


def workflow_payload(
    workflow: Any,
    tasks_by_step: dict[str, Any],
    unseen_attention: dict[str, int],
    latest_runs: dict[str, Any],
) -> dict[str, Any]:
    """One workflow, with each step's task and its latest state.

    The graph is sent as the client needs it to draw: every step names its
    upstreams by key, so the client never has to parse a signal name to work
    out what depends on what.  The task ids travel alongside because the run
    history, the cancel button and the output links are all keyed by them.
    """
    steps: list[dict[str, Any]] = []
    attention = 0
    for step in workflow.steps:
        key = str(step.key).strip()
        task = tasks_by_step.get(key)
        task_id = task.id if task is not None else ""
        unseen = int(unseen_attention.get(task_id, 0)) if task_id else 0
        latest = latest_runs.get(task_id) if task_id else None
        attention += unseen
        steps.append(
            {
                "key": key,
                "name": step.name,
                "kind": step.kind,
                "payload": step.payload,
                "depends_on": list(step.depends_on),
                "trigger_type": (
                    step.trigger.trigger_type if step.trigger is not None else "signal"
                ),
                "trigger": step.trigger.payload if step.trigger is not None else {},
                "workspace_root": step.workspace_root,
                "permission_profile": step.permission_profile,
                "acceptance": acceptance_payload(getattr(step, "acceptance", None)),
                # Sent back because a field that can be set and not read is a
                # field the editor has to remember for itself -- and the next
                # save, which resends what it was shown, would quietly reset it.
                "retry_policy": dict(getattr(step, "retry_policy", None) or {}),
                "timeout_seconds": int(step.timeout_seconds),
                "task_id": task_id,
                "enabled": bool(task.enabled) if task is not None else False,
                "unseen_attention": unseen,
                "in_flight": task_in_flight(task, latest) if task is not None else False,
                "latest_run": (
                    scheduler_run_payload(latest, with_snapshot=False)
                    if latest
                    else None
                ),
            }
        )
    return {
        "id": workflow.id,
        "name": workflow.name,
        "description": workflow.description,
        "enabled": bool(workflow.enabled),
        # The sentence that asked for the chain, which its steps inherit.
        # Sent here rather than repeated on every step because the chain is
        # what was asked for -- the steps are how it runs.
        "request_quote": str(getattr(workflow, "request_quote", "") or ""),
        "created_at": (
            workflow.created_at.isoformat() if workflow.created_at else None
        ),
        "updated_at": (
            workflow.updated_at.isoformat() if workflow.updated_at else None
        ),
        "steps": steps,
        "unseen_attention": attention,
    }


def attention_rows(store: Any, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """The snapshot's runs as rows a page can recognise and reopen.

    Each row carries where the run came from -- the task, and the workflow
    and step when it has one -- because a failure that belongs to a step of a
    deleted workflow is otherwise a row with no home, which is how a count
    becomes impossible to find.  The reason a run is asking is left to the
    client's own wording: the sentence already exists there next to the
    status labels it quotes, and a second copy here would be the thing that
    starts saying something else.
    """
    tasks = {task.id: task for task in store.list_tasks()}
    workflows = {workflow.id: workflow for workflow in store.list_workflows()}
    items = []
    for run in snapshot["runs"]:
        task = tasks.get(run.task_id)
        workflow_id = str(getattr(task, "workflow_id", "") or "")
        workflow = workflows.get(workflow_id)
        items.append(
            {
                "run_id": run.id,
                "task_id": run.task_id,
                "task_name": getattr(task, "name", "") or run.task_id,
                "workflow_id": workflow_id,
                "workflow_name": workflow.name if workflow is not None else "",
                # A step whose workflow is gone is not a step of nothing: it is
                # a step of something nobody can open, and the list has to be
                # able to say so rather than show a blank.
                "workflow_deleted": bool(workflow_id) and workflow is None,
                "step_key": str(getattr(task, "step_key", "") or ""),
                "status": run.status,
                "missed_count": int(getattr(run, "missed_count", 0) or 0),
                "error": str(getattr(run, "error", "") or ""),
                "started_at": (
                    run.started_at.isoformat() if run.started_at else None
                ),
                "finished_at": (
                    run.finished_at.isoformat() if run.finished_at else None
                ),
            }
        )
    return items


def attention_payload(store: Any) -> dict[str, Any]:
    """The number on the badge and the list behind it, from one snapshot.

    Sent together because they are the same fact at two sizes, and two facts
    is what a badge that disagrees with its own page is.  The number and the
    rows come out of the store's :meth:`attention_snapshot` — one query under
    one lock — so a run finishing between two reads cannot make the payload
    disagree with itself.

    ``latest_run_by_task`` answers "which run do I open for this task" for
    every task the count includes, including the ones whose rows fell out of
    the capped list -- where the list's own first row would be missing and
    the caller would be back to opening the newest run, the one that is
    usually fine.
    """
    snapshot = store.attention_snapshot()
    return {
        "unseen_attention": snapshot["total"],
        "attention_runs": attention_rows(store, snapshot),
        "latest_run_by_task": snapshot["latest_by_task"],
    }


def scheduler_output_path(task_id: str, run: Any) -> Path | None:
    raw = str(getattr(run, "output_path", "") or "").strip()
    if not raw:
        return None
    try:
        resolved = Path(raw).expanduser().resolve()
    except OSError:
        return None
    # Standalone scheduler output is always <output-root>/<task-id>/<run-id>.md.
    # Validate that shape so an unexpected database value cannot expose an
    # unrelated local file through the result endpoint.
    if resolved.parent.name != task_id or resolved.name != f"{run.id}.md":
        return None
    return resolved

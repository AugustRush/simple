"""Creating, editing and deleting workflows: materializing a definition into runs and steps, closing the rounds an edit invalidates, and the step list a workflow carries. Moved out of SchedulerStore (see the remaining-big-files survey work order)."""

from __future__ import annotations
import json

from agent.scheduler.rows import _step_task_spec, _step_workspaces, _task_from_row, _validate_graph
from datetime import datetime
from . import txn_helpers, workflow_engine
from .models import ScheduledTask, UTC, Workflow, _iso, step_trigger_spec, validate_workflow_graph

from pathlib import Path

from typing import Any, Optional

__all__ = [
    "delete_workflow",
    "update_workflow",
    "_materialize_workflow_in_transaction",
    "_close_rounds_before_edit",
    "step_tasks",
]


def _close_rounds_before_edit(
    conn,
    workflow_id: str,
    now: datetime,
) -> None:
    """End every round that is not executing something.  Caller owns txn.

        An edit may proceed while a round is ``running`` only if that round has
        no queued or running work: work in flight is executing the definition
        being replaced, and refusing there is the point of the check.  A round
        whose remaining steps are simply not coming -- the second clock entry
        of a dual-entry workflow, a join waiting on an upstream nobody will
        run -- has nothing in flight and can never complete on its own, so
        leaving it ``running`` turned one manual run into an edit lock that
        only deleting the workflow could open.  It is closed here, interrupted,
        because that is what it is: an execution the scheduler stopped being
        able to finish.

        Deliberately not a periodic sweep.  A round with nothing in flight is
        still rescuable -- manually running the upstream its join waits on
        joins that round (see :meth:`_round_waiting_on`) -- so closing it on a
        timer would take that away.  It is closed when somebody edits the
        definition, which is the moment the old round is declared over.
        """
    rounds = conn.execute(
        "SELECT id FROM workflow_runs WHERE workflow_id = ? AND status = 'running'",
        (workflow_id,),
    ).fetchall()
    for row in rounds:
        round_id = str(row["id"])
        workflow_engine._settle_workflow_run(conn, round_id, now)
        live = conn.execute(
            """
                SELECT 1 FROM scheduled_task_runs
                WHERE workflow_run_id = ? AND status IN ('queued', 'running')
                LIMIT 1
                """,
            (round_id,),
        ).fetchone()
        if live is not None:
            raise ValueError(
                "workflow 仍有执行中的轮次，需等待完成或取消后再修改定义"
            )
        conn.execute(
            """
                UPDATE workflow_runs
                SET status = 'interrupted', updated_at = ?, finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
            (_iso(now), _iso(now), round_id),
        )
        # Half-collected arrivals belong to the round that just ended, and
        # the graph they were waiting on is about to be replaced.  Left
        # behind they would complete a round nobody opened, on a report
        # from before the edit.
        conn.execute(
            "DELETE FROM workflow_signal_joins WHERE workflow_run_id = ?",
            (round_id,),
        )


def update_workflow(
    conn,
    workflow_id: str,
    workflow: Workflow,
    *,
    now: Optional[datetime] = None,
    materialize: bool = True,
) -> Optional[Workflow]:
    """Store a new graph, and by default rebuild the tasks behind it.

        ``materialize=False`` is for the one caller that has just written a
        step's *task* and is copying that change back into the graph: the task
        is the newer of the two, and rebuilding it from the step that was
        copied from it would be a round trip that only risks losing something.

        The new graph and the tasks rebuilt from it are one transaction, so an
        edit that fails halfway leaves the previous version running rather than
        a graph whose steps disagree with the tasks behind them.
        """
    if workflow_engine.get_workflow(conn, workflow_id) is None:
        return None
    _validate_graph(workflow.steps)
    updated_at = (now or datetime.now(UTC)).astimezone(UTC)
    with workflow_engine._immediate_transaction(conn):
        _close_rounds_before_edit(conn, workflow_id, updated_at)
        # `request_quote` is deliberately absent here.  It records who asked
        # for the chain, and editing a step does not change that -- while a
        # caller that rebuilds the graph from an edit form has no quote to
        # send, so writing the column from `workflow` would blank the one
        # piece of evidence the row holds about its own origin.
        conn.execute(
            """
                UPDATE workflows
                SET name = ?, description = ?, enabled = ?, graph_json = ?,
                    version = version + 1, updated_at = ?
                WHERE id = ?
                """,
            (
                workflow.name,
                workflow.description,
                1 if workflow.enabled else 0,
                json.dumps(workflow.to_graph(), ensure_ascii=False),
                _iso(updated_at),
                workflow_id,
            ),
        )
        if materialize:
            _materialize_workflow_in_transaction(conn, workflow_id, now=updated_at)
    return workflow_engine.get_workflow(conn, workflow_id)


def delete_workflow(
    conn,
    workflow_id: str,
    *,
    now: Optional[datetime] = None,
) -> list[str]:
    """Drop the graph and disable the tasks it built.  Returns their ids.

        The tasks are disabled rather than deleted, and their run history is
        left where it is.  Deleting them would take the record of what the
        workflow did with it, and a workflow is exactly the kind of thing
        somebody deletes in order to stop it -- not in order to forget that it
        ran.  Disabling also stops any subscription they hold from firing.

        A join that was part-way through its round does *not* keep that
        progress.  Unlike the run history, a half-satisfied round is not a
        record of something that happened -- it is an expectation about what
        happens next, and the graph it was waiting on no longer exists.  Left
        behind, it survives until somebody switches one of these orphaned
        steps back on (which they may, since a step whose workflow is gone is
        an ordinary task again), and then the first report from any one arm
        finds the set already full and closes a round whose other arms are
        still disabled and can never report.
        """
    now_dt = (now or datetime.now(UTC)).astimezone(UTC)
    disabled = [task.id for task in step_tasks(conn, workflow_id).values()]
    with workflow_engine._immediate_transaction(conn):
        if disabled:
            placeholders = ", ".join("?" for _ in disabled)
            conn.execute(
                f"""
                    UPDATE scheduled_task_runs
                    SET status = 'cancelled', finished_at = ?, updated_at = ?,
                        error = CASE WHEN error = '' THEN 'workflow deleted' ELSE error END
                    WHERE task_id IN ({placeholders}) AND status = 'queued'
                    """,
                (_iso(now_dt), _iso(now_dt), *disabled),
            )
            conn.execute(
                f"""
                    UPDATE scheduled_task_runs
                    SET cancel_requested_at = COALESCE(cancel_requested_at, ?),
                        updated_at = ?
                    WHERE task_id IN ({placeholders}) AND status = 'running'
                    """,
                (_iso(now_dt), _iso(now_dt), *disabled),
            )
        conn.execute(
            """
                UPDATE workflow_runs
                SET status = 'cancelled', updated_at = ?, finished_at = ?
                WHERE workflow_id = ? AND status = 'running'
                """,
            (_iso(now_dt), _iso(now_dt), workflow_id),
        )
        for task_id in disabled:
            conn.execute(
                """
                    UPDATE scheduled_tasks
                    SET enabled = 0, next_run_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                (_iso(now_dt), task_id),
            )
            conn.execute(
                "DELETE FROM signal_joins WHERE task_id = ?", (task_id,)
            )
            conn.execute(
                "DELETE FROM workflow_signal_joins WHERE task_id = ?", (task_id,)
            )
        conn.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
    return sorted(disabled)


def step_tasks(
    conn,
    workflow_id: str,
) -> dict[str, ScheduledTask]:
    """The tasks behind a workflow's steps, keyed by step key."""
    rows = conn.execute(
        """
            SELECT * FROM scheduled_tasks
            WHERE workflow_id = ? AND step_key != ''
            ORDER BY created_at ASC
            """,
        (workflow_id,),
    ).fetchall()
    return {row["step_key"]: _task_from_row(row) for row in rows}


def _materialize_workflow_in_transaction(
    conn,
    workflow_id: str,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Body of :meth:`materialize_workflow`.  Caller owns the transaction.

        Split out so that storing a graph and building the tasks behind it can
        be one transaction rather than two.  The graph row is what makes a
        workflow visible; committing it before its tasks exist leaves, for as
        long as the build takes and forever if the build fails, exactly the
        drawing that :meth:`create_workflow` exists to avoid.
        """
    workflow = workflow_engine.get_workflow(conn, workflow_id)
    if workflow is None:
        raise ValueError(f"找不到 workflow：{workflow_id}")
    order = validate_workflow_graph(workflow.steps)
    now_dt = (now or datetime.now(UTC)).astimezone(UTC)
    by_key = {str(step.key).strip(): step for step in workflow.steps}
    existing = step_tasks(conn, workflow_id)
    workspaces, took_inherited = _step_workspaces(workflow.steps, order)

    created: list[str] = []
    updated: list[str] = []
    inherited: list[str] = []
    task_id_by_key: dict[str, str] = {}
    removed: list[str] = []
    # One transaction for the whole graph.  The workflow row is written
    # before any of its tasks exist, so a failure partway through this loop
    # would leave a graph whose later steps have nothing to run them, while
    # the steps that did get built are already subscribed to task ids the
    # rest of the graph will never be told about.  A half-materialized
    # workflow looks enabled and fires its head, then stops silently.
    for key in order:
        step = by_key[key]
        upstream_ids = [task_id_by_key[dep] for dep in step.depends_on]
        trigger = step_trigger_spec(step, upstream_ids)
        spec = _step_task_spec(
            step,
            trigger,
            workflow_id=workflow_id,
            enabled=bool(workflow.enabled),
            workspace=workspaces.get(key) or str(Path.cwd().resolve()),
            request_quote=str(getattr(workflow, "request_quote", "") or ""),
        )
        if key in took_inherited:
            inherited.append(key)
        current = existing.get(key)
        if current is None:
            task_id = txn_helpers._create_task_in_transaction(conn, spec, now=now_dt)
            created.append(task_id)
        else:
            txn_helpers._update_task_in_transaction(conn, current.id, spec, now=now_dt)
            task_id = current.id
            updated.append(task_id)
        task_id_by_key[key] = task_id

    # A step that is no longer in the graph must stop running.  Its task
    # and its history stay; what goes is its ability to fire.
    for key, task in existing.items():
        if key in task_id_by_key:
            continue
        if task.enabled:
            conn.execute(
                """
                    UPDATE scheduled_tasks
                    SET enabled = 0, next_run_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                (_iso(now_dt), task.id),
            )
        removed.append(key)

    return {
        "workflow_id": workflow_id,
        "order": order,
        "created": created,
        "updated": updated,
        "removed": sorted(removed),
        "inherited_workspace": sorted(inherited),
        "tasks_by_step": dict(task_id_by_key),
    }

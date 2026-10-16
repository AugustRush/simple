"""The transaction helpers: tasks, emissions, deliveries, joins, workflow-run steps.

Moved out of ``SchedulerStore`` (see the remaining-big-files survey).  Every function here runs
inside a transaction its **caller** owns -- the store's ``_immediate_transaction`` -- so none of
them opens a connection: the connection arrives as the first argument.  The class keeps a
one-line wrapper per helper, and those wrappers are also where the ``@_synchronized`` lock stays
for the methods that had one, because a module function wearing that decorator would be called
with ``self`` bound to the connection.

Nothing here imports ``agent.scheduler.store``: the dependency runs one way.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from agent.scheduler.rows import _check_acceptance, _check_products, _task_from_row

from .models import (
    UTC,
    NewScheduledTask,
    ScheduledTask,
    SignalEmission,
    Workflow,
    WorkflowStep,
    _iso,
    _new_id,
    declared_products,
    optional_datetime,
    subscribes_to,
)

__all__ = [
    "_create_task_in_transaction",
    "_insert_emission",
    "_update_task_in_transaction",
    "_workflow_run_steps",
    "_join_arrivals",
    "_satisfied_joins",
    "_signal_subscribers",
    "_record_delivery",
    "_pending_run_for",
    "_settle_emission",
]


def _create_task_in_transaction(
    conn,
    task: NewScheduledTask,
    *,
    now: Optional[datetime] = None,
) -> str:
    """Insert one task and return its id.  Caller owns the transaction.

        Split out so that building the several tasks of a workflow can be one
        transaction: a graph is stored together with the tasks behind it, and a
        failure partway through must leave neither rather than a graph whose
        last steps have nothing to run them.
        """
    _check_acceptance(task)
    _check_products(task)
    created_at = (now or datetime.now(UTC)).astimezone(UTC)
    task_id = _new_id()
    next_run_at = task.trigger.initial_run_at(created_at)
    conn.execute(
        """
            INSERT INTO scheduled_tasks (
                id, name, kind, enabled, trigger_json, payload_json,
                delivery_mode, delivery_target_json, model_override,
                overlap_policy, missed_run_policy, workspace_root,
                context_policy, timeout_seconds, retry_policy_json,
                selected_skills_json,
                permission_profile,
                acceptance_json,
                produces_json,
                next_run_at, lease_until,
                active_run_id, last_run_at, last_success_at, created_at, updated_at,
                workflow_id, step_key, request_quote
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?)
            """,
        (
            task_id,
            task.name,
            task.kind,
            1 if task.enabled else 0,
            task.trigger.to_json(),
            json.dumps(task.payload, ensure_ascii=False),
            task.delivery_mode,
            task.delivery_target.to_json(),
            task.model_override,
            "forbid_overlap",
            "coalesce",
            task.workspace_root,
            task.context_policy,
            int(task.timeout_seconds),
            json.dumps(task.retry_policy, ensure_ascii=False),
            json.dumps(task.selected_skills, ensure_ascii=False),
            task.permission_profile,
            task.acceptance.to_json(),
            json.dumps(declared_products(task), ensure_ascii=False),
            _iso(next_run_at),
            _iso(created_at),
            _iso(created_at),
            task.workflow_id,
            task.step_key,
            str(getattr(task, "request_quote", "") or ""),
        ),
    )
    return task_id


def _insert_emission(
    conn,
    name: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    source: str = "manual",
    depth: int = 0,
    origin_id: str = "",
    now: datetime,
) -> SignalEmission:
    """Insert one pending emission.  The caller owns the transaction.

        Split out of :meth:`emit_signal` so that a run reaching a terminal
        state can record its emission inside the transaction that records the
        status.  A run recorded as finished and a signal recorded as emitted
        then commit together, which is what makes "it succeeded and nothing
        downstream ever heard about it" impossible rather than merely unlikely.
        """
    normalized = str(name or "").strip()
    if not normalized:
        raise ValueError("signal name cannot be empty")
    emission_id = _new_id()
    # A root emission is its own origin, so every cascade has exactly one
    # id to group by without a second column meaning "no parent".
    root = str(origin_id or "").strip() or emission_id
    settled_depth = max(0, int(depth))
    conn.execute(
        """
            INSERT INTO signal_emissions (
                id, name, payload_json, source, depth, origin_id,
                state, reason, delivered_run_id, created_at, delivered_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', '', '', ?, NULL)
            """,
        (
            emission_id,
            normalized,
            json.dumps(payload or {}, ensure_ascii=False),
            str(source or "manual"),
            settled_depth,
            root,
            _iso(now),
        ),
    )
    return SignalEmission(
        id=emission_id,
        name=normalized,
        payload=dict(payload or {}),
        source=str(source or "manual"),
        depth=settled_depth,
        origin_id=root,
        state="pending",
        created_at=now,
    )


def _signal_subscribers(
    conn,
    name: str,
) -> list[ScheduledTask]:
    """Enabled tasks waiting on this exact name.

        Read by scanning the enabled tasks rather than by an index on the
        trigger, because the subscription lives inside ``trigger_json``.  At
        the scale this runs at -- the tasks of one person -- that is a handful
        of rows per delivery.  A repository with thousands of tasks would want
        subscriptions broken out into their own table; that is a change worth
        making when the row count says so, not before.

        A join (``mode="all"``) is a subscriber too, but it is *not* run by
        this emission alone: the caller has to accumulate, which is what
        :meth:`_advance_join` is for.  Returning it here keeps "who is
        listening" a single question with a single answer, and the delivery
        loop decides what each answer means.
        """
    subscribers: list[ScheduledTask] = []
    rows = conn.execute(
        "SELECT * FROM scheduled_tasks WHERE enabled = 1"
    ).fetchall()
    for row in rows:
        task = _task_from_row(row)
        if subscribes_to(task.trigger, name):
            subscribers.append(task)
    return subscribers


def _satisfied_joins(
    conn,
    task_id: str,
    workflow_run_id: str = "",
) -> set[str]:
    if workflow_run_id:
        row = conn.execute(
            """
                SELECT satisfied_json FROM workflow_signal_joins
                WHERE task_id = ? AND workflow_run_id = ?
                """,
            (task_id, workflow_run_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT satisfied_json FROM signal_joins WHERE task_id = ?", (task_id,)
        ).fetchone()
    if row is None:
        return set()
    try:
        decoded = json.loads(row["satisfied_json"] or "[]")
    except (TypeError, ValueError):
        return set()
    if not isinstance(decoded, list):
        return set()
    return {str(item) for item in decoded if str(item).strip()}


def _join_arrivals(
    conn,
    task_id: str,
    workflow_run_id: str = "",
) -> dict[str, dict[str, Any]]:
    """The payloads a join has collected since its last completed round."""
    if workflow_run_id:
        row = conn.execute(
            """
                SELECT arrivals_json FROM workflow_signal_joins
                WHERE task_id = ? AND workflow_run_id = ?
                """,
            (task_id, workflow_run_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT arrivals_json FROM signal_joins WHERE task_id = ?", (task_id,)
        ).fetchone()
    if row is None:
        return {}
    try:
        decoded = json.loads(row["arrivals_json"] or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {
        str(name): dict(payload)
        for name, payload in decoded.items()
        if isinstance(payload, dict)
    }


def _record_delivery(
    conn,
    emission_id: str,
    task_id: str,
    outcome: str,
    *,
    run_id: str = "",
    reason: str = "",
    now: datetime,
) -> None:
    conn.execute(
        """
            INSERT INTO signal_deliveries (
                id, emission_id, task_id, run_id, outcome, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
        (_new_id(), emission_id, task_id, run_id, outcome, reason, _iso(now)),
    )


def _pending_run_for(
    conn,
    task_id: str,
    workflow_run_id: str = "",
) -> Optional[str]:
    """The run already waiting for, or running, this task, if any."""
    workflow_clause = ""
    params: list[Any] = [task_id]
    if workflow_run_id:
        workflow_clause = " AND workflow_run_id = ?"
        params.append(workflow_run_id)
    row = conn.execute(
        f"""
            SELECT id FROM scheduled_task_runs
            WHERE task_id = ? AND status IN ('queued', 'running')
              {workflow_clause}
            ORDER BY created_at ASC
            LIMIT 1
            """,
        params,
    ).fetchone()
    return row["id"] if row else None


def _settle_emission(
    conn,
    emission: SignalEmission,
    *,
    state: str,
    reason: str,
    now: datetime,
    run_id: str = "",
) -> None:
    conn.execute(
        """
            UPDATE signal_emissions
            SET state = ?, reason = ?, delivered_run_id = ?, delivered_at = ?
            WHERE id = ? AND state = 'pending'
            """,
        (state, reason, run_id, _iso(now), emission.id),
    )


def _update_task_in_transaction(
    conn,
    task_id: str,
    task: NewScheduledTask,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Rewrite one task.  False when there was no such row.

        Caller owns the transaction, for the same reason
        :meth:`_create_task_in_transaction` exists: refreshing a workflow
        rewrites every one of its steps, and half a refresh is a graph whose
        steps disagree with each other about what the graph says.
        """
    _check_acceptance(task)
    _check_products(task)
    updated_at = (now or datetime.now(UTC)).astimezone(UTC)
    trigger_json = task.trigger.to_json()
    # Only a changed trigger gets a new schedule.  Recomputing it on every
    # edit would make renaming a task -- or saving the workflow it is a
    # step of, which rewrites every step -- push its next occurrence into
    # the future, silently swallowing one that was already due.  What goes
    # with it is worse than the run: an occurrence missed while the gateway
    # was down is reported through ``missed_count``, and
    # ``_attention_clause`` counts that as the *only* way such a gap is
    # ever mentioned -- the run that resumes the schedule reports success.
    # So an unrelated rename could erase the single trace of a daily report
    # that has been missing for a week.
    #
    # A stored ``NULL`` is the exception: that is what disabling writes, so
    # a task coming back on has no schedule to keep and needs one computed
    # even though its trigger never changed.  Keeping the ``NULL`` would
    # re-enable a task that can never fire again.
    current = conn.execute(
        "SELECT trigger_json, next_run_at FROM scheduled_tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    keeps_schedule = (
        current is not None
        and str(current["trigger_json"] or "") == trigger_json
        and (current["next_run_at"] is not None or not task.enabled)
    )
    if keeps_schedule:
        next_run_at = optional_datetime(current["next_run_at"])
    else:
        next_run_at = task.trigger.initial_run_at(updated_at)
    cursor = conn.execute(
        """
            UPDATE scheduled_tasks
            SET name = ?, kind = ?, enabled = ?, trigger_json = ?,
                payload_json = ?, delivery_mode = ?, delivery_target_json = ?,
                model_override = ?, overlap_policy = ?, missed_run_policy = ?,
                workspace_root = ?, context_policy = ?, timeout_seconds = ?,
                retry_policy_json = ?, next_run_at = ?, updated_at = ?
                , selected_skills_json = ?, permission_profile = ?
                , acceptance_json = ?
                , produces_json = ?
                , workflow_id = ?, step_key = ?
            WHERE id = ?
            """,
        (
            task.name,
            task.kind,
            1 if task.enabled else 0,
            trigger_json,
            json.dumps(task.payload, ensure_ascii=False),
            task.delivery_mode,
            task.delivery_target.to_json(),
            task.model_override,
            "forbid_overlap",
            "coalesce",
            task.workspace_root,
            task.context_policy,
            int(task.timeout_seconds),
            json.dumps(task.retry_policy, ensure_ascii=False),
            _iso(next_run_at),
            _iso(updated_at),
            json.dumps(task.selected_skills, ensure_ascii=False),
            task.permission_profile,
            task.acceptance.to_json(),
            # Rewritten with the definition, so a step whose promised
            # products were edited starts promising the new ones -- and,
            # because the contract travels on the run's snapshot, whatever
            # is already in flight keeps the promise it was started with.
            json.dumps(declared_products(task), ensure_ascii=False),
            task.workflow_id,
            task.step_key,
            task_id,
        ),
    )
    return bool(cursor.rowcount)


def _workflow_run_steps(
    conn,
    workflow_run_id: str,
) -> list[WorkflowStep]:
    if not workflow_run_id:
        return []
    row = conn.execute(
        "SELECT definition_snapshot_json FROM workflow_runs WHERE id = ?",
        (workflow_run_id,),
    ).fetchone()
    if row is None:
        return []
    try:
        return Workflow.from_graph(row["definition_snapshot_json"]).steps
    except (TypeError, ValueError, json.JSONDecodeError):
        return []

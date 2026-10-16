"""Claiming and releasing a task, and the retry ladder: how a task is taken inside a transaction, when a failed run is retried, and how a claim is handed back. Moved out of SchedulerStore (see the remaining-big-files survey work order)."""

from __future__ import annotations
import json

from typing import Optional
from agent.scheduler.rows import _run_from_row, _task_from_row
from datetime import datetime, timedelta
from . import attention, workflow_engine
from .models import ClaimedTask, ScheduledTask, TaskRun, UTC, _iso, _new_id, execution_snapshot

__all__ = [
    "_claim_task_in_transaction",
    "claim_retry",
    "enqueue_retry",
    "release_claim",
]


def _claim_task_in_transaction(
    conn,
    task: ScheduledTask,
    *,
    now: datetime,
    lease_seconds: int,
    scheduled_for: datetime,
    trigger_source: str,
    snapshot: dict,
    attempt: int = 1,
) -> Optional[ClaimedTask]:
    run_id = _new_id()
    lease_until = now + timedelta(seconds=lease_seconds)
    # A run started by hand or by a retry is owed the same picture of the
    # steps above it as one a signal woke.  Only filled when nothing else
    # has: a snapshot that already carries ``signals`` came from the
    # emission that queued this run and describes that round exactly,
    # which is more faithful than re-deriving it here.  ``signal`` is
    # deliberately left unset -- that key means "the emission that woke
    # this run" and is where cascade depth is inherited from, and nothing
    # woke this one.
    snapshot = dict(snapshot or {})
    if "signals" not in snapshot:
        arrivals = attention._upstream_reports_in_transaction(conn, task)
        if arrivals:
            snapshot["signals"] = [
                {"name": name, "payload": payload}
                for name, payload in sorted(arrivals.items())
            ]
    snapshot, workflow_run_id = workflow_engine._prepare_workflow_run(
        conn, task,
        run_id,
        snapshot,
        now=now,
        trigger_source=trigger_source,
    )
    conn.execute(
        """
            INSERT INTO scheduled_task_runs (
                id, task_id, scheduled_for, started_at, finished_at, status,
                summary, error, output_path, delivery_status,
                config_snapshot_json, trigger_source, attempt,
                workflow_run_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, 'running', '', '', '', '', ?, ?, ?, ?, ?, ?)
            """,
        (
            run_id,
            task.id,
            _iso(scheduled_for),
            _iso(now),
            json.dumps(snapshot, ensure_ascii=False),
            trigger_source,
            max(1, int(attempt)),
            workflow_run_id,
            _iso(now),
            _iso(now),
        ),
    )
    cursor = conn.execute(
        """
            UPDATE scheduled_tasks
            SET lease_until = ?, active_run_id = ?, updated_at = ?
            WHERE id = ? AND active_run_id IS NULL
            """,
        (_iso(lease_until), run_id, _iso(now), task.id),
    )
    if cursor.rowcount != 1:
        conn.execute(
            "DELETE FROM scheduled_task_runs WHERE id = ?", (run_id,)
        )
        return None
    refreshed = conn.execute(
        "SELECT * FROM scheduled_tasks WHERE id = ?", (task.id,)
    ).fetchone()
    claimed_task = _task_from_row(refreshed) if refreshed else task
    return ClaimedTask(
        task=claimed_task,
        run=TaskRun(
            id=run_id,
            task_id=task.id,
            scheduled_for=scheduled_for,
            started_at=now,
            finished_at=None,
            status="running",
            config_snapshot=snapshot,
            trigger_source=trigger_source,
            attempt=max(1, int(attempt)),
            workflow_run_id=workflow_run_id,
            created_at=now,
            updated_at=now,
        ),
    )


def claim_retry(
    conn,
    task_id: str,
    run_id: str,
    *,
    use_latest: bool = False,
    now: Optional[datetime] = None,
    lease_seconds: int = 300,
) -> Optional[ClaimedTask]:
    if lease_seconds < 3:
        raise ValueError("lease_seconds must be at least 3")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    with workflow_engine._immediate_transaction(conn):
        workflow_engine._recover_stale_runs_in_transaction(conn, current)
        task_row = conn.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ? AND active_run_id IS NULL",
            (task_id,),
        ).fetchone()
        source_row = conn.execute(
            """
                SELECT * FROM scheduled_task_runs
                WHERE id = ? AND task_id = ? AND status <> 'running'
                """,
            (run_id, task_id),
        ).fetchone()
        if task_row is None or source_row is None:
            return None
        task = _task_from_row(task_row)
        source = _run_from_row(source_row)
        snapshot = (
            execution_snapshot(task) if use_latest else source.config_snapshot
        )
        if not snapshot:
            snapshot = execution_snapshot(task)
        return _claim_task_in_transaction(
            conn, task,
            now=current,
            lease_seconds=lease_seconds,
            scheduled_for=source.scheduled_for,
            trigger_source="retry_latest" if use_latest else "retry_snapshot",
            snapshot=snapshot,
            attempt=source.attempt + 1,
        )


def enqueue_retry(
    conn,
    task_id: str,
    source_run_id: str,
    *,
    retry_at: datetime,
) -> Optional[TaskRun]:
    retry_at = retry_at.astimezone(UTC)
    with workflow_engine._immediate_transaction(conn):
        source_row = conn.execute(
            """
                SELECT * FROM scheduled_task_runs
                WHERE id = ? AND task_id = ? AND status = 'failed'
                """,
            (source_run_id, task_id),
        ).fetchone()
        if source_row is None:
            return None
        existing = conn.execute(
            """
                SELECT * FROM scheduled_task_runs
                WHERE task_id = ? AND retry_of_run_id = ? AND status IN ('queued', 'running')
                LIMIT 1
                """,
            (task_id, source_run_id),
        ).fetchone()
        if existing is not None:
            return _run_from_row(existing)
        source = _run_from_row(source_row)
        run_id = _new_id()
        conn.execute(
            """
                INSERT INTO scheduled_task_runs (
                    id, task_id, scheduled_for, started_at, finished_at, status,
                    summary, error, output_path, delivery_status,
                    config_snapshot_json, trigger_source, attempt,
                    cancel_requested_at, retry_of_run_id, workflow_run_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, 'queued', '', '', '', '', ?,
                          'automatic_retry', ?, NULL, ?, ?, ?, ?)
                """,
            (
                run_id,
                task_id,
                _iso(source.scheduled_for),
                _iso(retry_at),
                json.dumps(source.config_snapshot, ensure_ascii=False),
                source.attempt + 1,
                source_run_id,
                source.workflow_run_id,
                _iso(datetime.now(UTC)),
                _iso(datetime.now(UTC)),
            ),
        )
        row = conn.execute(
            "SELECT * FROM scheduled_task_runs WHERE id = ?", (run_id,)
        ).fetchone()
    return _run_from_row(row) if row else None


def release_claim(
    conn,
    task_id: str,
    run_id: str,
    *,
    now: datetime,
    reason: str,
) -> bool:
    """Release an unexecuted claim without consuming its scheduled occurrence."""
    now = now.astimezone(UTC)
    with workflow_engine._immediate_transaction(conn):
        row = conn.execute(
            """
                SELECT r.scheduled_for
                FROM scheduled_tasks t
                JOIN scheduled_task_runs r
                  ON r.id = t.active_run_id AND r.task_id = t.id
                WHERE t.id = ? AND t.active_run_id = ? AND r.status = 'running'
                """,
            (task_id, run_id),
        ).fetchone()
        if row is None:
            return False
        task_cursor = conn.execute(
            """
                UPDATE scheduled_tasks
                SET next_run_at = ?, lease_until = NULL, active_run_id = NULL,
                    updated_at = ?
                WHERE id = ? AND active_run_id = ?
                """,
            (row["scheduled_for"], _iso(now), task_id, run_id),
        )
        if task_cursor.rowcount != 1:
            return False
        conn.execute(
            """
                UPDATE scheduled_task_runs
                SET status = 'interrupted', error = ?, finished_at = ?, updated_at = ?
                WHERE id = ? AND task_id = ? AND status = 'running'
                """,
            (reason, _iso(now), _iso(now), run_id, task_id),
        )
        # A released claim is over: nothing completes it afterwards, so if
        # it was the only live work of its workflow round, the round is
        # rolled up here.  Same reasoning as the stale-lease recovery
        # above -- every way a run ends has to close the round it ends.
        workflow_engine._settle_workflow_run_of_run(conn, run_id, now)
    return True

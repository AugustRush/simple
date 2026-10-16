"""Reading the schedule store: tasks, runs, workflows, emissions, attention.

Moved out of ``SchedulerStore`` (see the remaining-big-files survey).  These methods do not open
a connection -- the store's callers own the transaction, and the connection arrives as
``self._conn`` -- so a function here takes that connection as its first argument and reads.  The
class keeps a one-line wrapper per reader and, where it had one, keeps its ``@_synchronized``
decorator: the lock is the store's, and a module function decorated with it would be called with
``self`` bound to the connection.
"""

from __future__ import annotations

from typing import Optional

from agent.scheduler.rows import _run_from_row, _task_from_row

from .models import ScheduledTask, TaskRun

__all__ = [
    "get_task",
    "list_tasks",
    "get_run",
    "list_runs",
]


def list_tasks(conn) -> list[ScheduledTask]:
    rows = conn.execute(
        "SELECT * FROM scheduled_tasks ORDER BY created_at ASC"
    ).fetchall()
    return [_task_from_row(row) for row in rows]


def get_task(
    conn,
    task_id: str,
) -> Optional[ScheduledTask]:
    row = conn.execute(
        "SELECT * FROM scheduled_tasks WHERE id = ? LIMIT 1",
        (task_id,),
    ).fetchone()
    return _task_from_row(row) if row else None


def list_runs(
    conn,
    task_id: str,
) -> list[TaskRun]:
    rows = conn.execute(
        """
            SELECT * FROM scheduled_task_runs
            WHERE task_id = ?
            ORDER BY created_at ASC, id ASC
            """,
        (task_id,),
    ).fetchall()
    return [_run_from_row(row) for row in rows]


def get_run(
    conn,
    task_id: str,
    run_id: str,
) -> Optional[TaskRun]:
    row = conn.execute(
        """
            SELECT * FROM scheduled_task_runs
            WHERE task_id = ? AND id = ?
            LIMIT 1
            """,
        (task_id, run_id),
    ).fetchone()
    return _run_from_row(row) if row else None

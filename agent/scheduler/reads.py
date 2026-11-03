"""Reading the schedule store: tasks, runs, workflows, emissions, attention.

Moved out of ``SchedulerStore`` (see the remaining-big-files survey).  These methods do not open
a connection -- the store's callers own the transaction, and the connection arrives as
``self._conn`` -- so a function here takes that connection as its first argument and reads.  The
class keeps a one-line wrapper per reader and, where it had one, keeps its ``@_synchronized``
decorator: the lock is the store's, and a module function decorated with it would be called with
``self`` bound to the connection.
"""

from __future__ import annotations

from typing import Any, Optional

from agent.scheduler.rows import _run_from_row, _task_from_row

from .models import ScheduledTask, TaskRun

__all__ = [
    "get_task",
    "list_tasks",
    "get_run",
    "list_runs",
    "recent_runs",
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


def recent_runs(
    conn,
    limit: int = 20,
    *,
    before: Optional[str] = None,
) -> list[tuple[TaskRun, ScheduledTask]]:
    """Runs across *all* tasks, newest first, with the task each belongs to.

    The gap this fills
    -----------------
    Every existing reader is per-task (``list_runs(task_id)``) or per-problem
    (``attention_snapshot`` returns only unacknowledged failures). So no caller
    could answer "what has this agent done lately" -- the one question a person
    opening the app has about the agent itself, as opposed to about a task they
    already know the name of.

    Two queries, not a JOIN: the two tables share column names (``id``,
    ``created_at``, ``updated_at``), and a JOIN has to alias every one of them for
    both row mappers to read their own record. Aliasing them all is longer, and a
    forgotten alias fails at the mapper with a key error rather than here. Two
    reads with one lookup table is the smaller thing.

    Ordered with running runs first: a run that is still going is the most current
    fact there is, and sorting it below finished work from yesterday would be
    wrong twice.
    """
    # ``before`` is a cursor, not an offset: the feed is append-only at the front,
    # so an offset would repeat or skip rows whenever a run finished between two
    # page loads -- exactly while somebody is reading the page.
    where = "status IN ('queued', 'running') OR finished_at IS NOT NULL"
    params: list[Any] = []
    if before:
        where += " AND finished_at IS NOT NULL AND finished_at < ?"
        params.append(str(before))
    rows = conn.execute(
        f"""
            SELECT * FROM scheduled_task_runs
            WHERE {where}
            ORDER BY
                CASE WHEN status IN ('queued', 'running') THEN 0 ELSE 1 END,
                finished_at DESC,
                created_at DESC,
                id DESC
            LIMIT ?
            """,
        (*params, max(1, int(limit))),
    ).fetchall()
    if not rows:
        return []
    runs = [_run_from_row(row) for row in rows]
    task_ids = {run.task_id for run in runs}
    placeholders = ",".join("?" for _ in task_ids)
    task_rows = conn.execute(
        f"SELECT * FROM scheduled_tasks WHERE id IN ({placeholders})",
        tuple(task_ids),
    ).fetchall()
    tasks = {row["id"]: _task_from_row(row) for row in task_rows}
    return [(run, tasks[run.task_id]) for run in runs if run.task_id in tasks]


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

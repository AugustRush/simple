"""Putting an exported archive's scheduled work back.

Why this lives in the scheduler package
---------------------------------------
``tests/test_scheduler_store_seam.py`` refuses a ``SchedulerStore`` constructed
outside ``agent/scheduler``, and it is right to: "one construction site" stops
being true the moment a caller that only wants to write a few rows opens its own.
So the import lives here, next to the schema it writes into, and the exporter's
counterpart in ``agent/import_data.py`` calls it by name.

What "import" means here
------------------------
**Additive.** A task whose id is already present is skipped, never overwritten --
the same rule the conversation import follows, and for the same reason: replacing
what is on this machine is the accident the export exists to undo.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

__all__ = ["import_tasks"]


def import_tasks(db_path: Path, payload: Any) -> dict[str, int]:
    """Merge ``payload['tasks']`` into the scheduler store; return what happened."""
    if not isinstance(payload, dict):
        return {"tasks_added": 0, "tasks_skipped": 0}
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The store owns the schema; this function owns only the writes. Opening one
    # is what makes importing into a fresh install work -- measured: a raw
    # connection to a home that had never run the scheduler found no
    # `scheduled_tasks` table at all.
    from .store import SchedulerStore

    SchedulerStore(db_path=path).close()

    added = skipped = runs_added = 0
    conn = sqlite3.connect(str(path))
    try:
        with conn:
            # Runs first: a task's `active_run_id` refers to one, and the check
            # below needs the runs to exist before it can tell a live claim from
            # a stale one.
            for run in payload.get("runs") or []:
                if not isinstance(run, dict):
                    continue
                run_id = str(run.get("id") or "").strip()
                if not run_id:
                    continue
                if conn.execute(
                    "SELECT 1 FROM scheduled_task_runs WHERE id = ?", (run_id,)
                ).fetchone():
                    continue
                columns = [key for key in run if key != "id"]
                conn.execute(
                    f"INSERT INTO scheduled_task_runs (id, {', '.join(columns)}) "
                    f"VALUES (?, {', '.join('?' for _ in columns)})",
                    (run_id, *[run[column] for column in columns]),
                )
                runs_added += 1
            for task in payload.get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                task_id = str(task.get("id") or "").strip()
                if not task_id:
                    continue
                if conn.execute(
                    "SELECT 1 FROM scheduled_tasks WHERE id = ?", (task_id,)
                ).fetchone():
                    skipped += 1
                    continue
                columns = [key for key in task if key != "id"]
                values = [task[column] for column in columns]
                # A task exported while it was running carries an `active_run_id`
                # and a `lease_until`. If that run row is not in the archive, the
                # imported task points at nothing: `claim_due_tasks` requires
                # `active_run_id IS NULL` and stale-run recovery needs a run row
                # to join, so the task would never run again. Measured. Clearing
                # the claim is the only honest repair -- the work was not done.
                active = str(task.get("active_run_id") or "").strip()
                if active and not conn.execute(
                    "SELECT 1 FROM scheduled_task_runs WHERE id = ?", (active,)
                ).fetchone():
                    for name in ("active_run_id", "lease_until"):
                        if name in columns:
                            values[columns.index(name)] = None
                conn.execute(
                    f"INSERT INTO scheduled_tasks (id, {', '.join(columns)}) "
                    f"VALUES (?, {', '.join('?' for _ in columns)})",
                    (task_id, *values),
                )
                added += 1
    finally:
        conn.close()
    return {"tasks_added": added, "tasks_skipped": skipped, "task_runs_added": runs_added}

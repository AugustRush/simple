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

    added = skipped = 0
    conn = sqlite3.connect(str(path))
    try:
        with conn:
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
                conn.execute(
                    f"INSERT INTO scheduled_tasks (id, {', '.join(columns)}) "
                    f"VALUES (?, {', '.join('?' for _ in columns)})",
                    (task_id, *[task[column] for column in columns]),
                )
                added += 1
    finally:
        conn.close()
    return {"tasks_added": added, "tasks_skipped": skipped}

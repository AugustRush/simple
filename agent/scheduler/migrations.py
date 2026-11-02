"""The schema migrations of the scheduler store, in order.

Moved out of ``agent/scheduler/store.py`` (see the remaining-big-files survey): they were
already module-level functions needing only ``sqlite3``, so this is a move rather than an
extraction.  They stay imported into the store's namespace, because the migration table there
maps version numbers to these names and that table is the thing that says what order they run
in -- which is the part that belongs to the store.

Each takes the connection and brings the schema from *version - 1* to *version*; none of them
reads anything else, which is what makes one testable against a copied database.
"""

from __future__ import annotations

import sqlite3

__all__ = ['_migrate_to_1', '_migrate_to_2', '_migrate_to_3', '_migrate_to_4', '_migrate_to_5', '_migrate_to_6', '_migrate_to_7', '_migrate_to_8', '_migrate_to_9', '_migrate_to_10', '_migrate_to_11', '_migrate_to_12', '_migrate_to_13', '_migrate_to_14']


def _migrate_to_1(conn: sqlite3.Connection) -> None:
    pass


def _migrate_to_2(conn: sqlite3.Connection) -> None:
    task_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(scheduled_tasks)").fetchall()
    }
    run_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(scheduled_task_runs)").fetchall()
    }
    additions = {
        "workspace_root": "TEXT NOT NULL DEFAULT ''",
        "context_policy": "TEXT NOT NULL DEFAULT 'stateless'",
        "timeout_seconds": "INTEGER NOT NULL DEFAULT 1800",
        "retry_policy_json": (
            'TEXT NOT NULL DEFAULT \'{"max_attempts": 1, "backoff_seconds": 30}\''
        ),
    }
    for name, declaration in additions.items():
        if name not in task_columns:
            conn.execute(f"ALTER TABLE scheduled_tasks ADD COLUMN {name} {declaration}")
    run_additions = {
        "config_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
        "trigger_source": "TEXT NOT NULL DEFAULT 'schedule'",
        "attempt": "INTEGER NOT NULL DEFAULT 1",
        "cancel_requested_at": "TEXT",
    }
    for name, declaration in run_additions.items():
        if name not in run_columns:
            conn.execute(
                f"ALTER TABLE scheduled_task_runs ADD COLUMN {name} {declaration}"
            )


def _migrate_to_3(conn: sqlite3.Connection) -> None:
    run_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(scheduled_task_runs)").fetchall()
    }
    if "retry_of_run_id" not in run_columns:
        conn.execute(
            "ALTER TABLE scheduled_task_runs ADD COLUMN "
            "retry_of_run_id TEXT NOT NULL DEFAULT ''"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scheduled_task_runs_queued "
        "ON scheduled_task_runs(status, started_at)"
    )


def _migrate_to_4(conn: sqlite3.Connection) -> None:
    task_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(scheduled_tasks)").fetchall()
    }
    if "selected_skills_json" not in task_columns:
        conn.execute(
            "ALTER TABLE scheduled_tasks ADD COLUMN "
            "selected_skills_json TEXT NOT NULL DEFAULT '[]'"
        )


def _migrate_to_5(conn: sqlite3.Connection) -> None:
    task_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(scheduled_tasks)").fetchall()
    }
    if "permission_profile" not in task_columns:
        conn.execute(
            "ALTER TABLE scheduled_tasks ADD COLUMN "
            "permission_profile TEXT NOT NULL DEFAULT 'inherit'"
        )


def _migrate_to_6(conn: sqlite3.Connection) -> None:
    run_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(scheduled_task_runs)").fetchall()
    }
    if "acknowledged_at" not in run_columns:
        conn.execute("ALTER TABLE scheduled_task_runs ADD COLUMN acknowledged_at TEXT")
        # Runs that predate this column were already reported
        # through the run list, which is how the user found out
        # about them.  Backfilling finished runs as seen keeps the
        # new counter from announcing a backlog of history on the
        # first launch after the upgrade.
        conn.execute(
            "UPDATE scheduled_task_runs SET acknowledged_at = finished_at "
            "WHERE finished_at IS NOT NULL"
        )


def _migrate_to_7(conn: sqlite3.Connection) -> None:
    run_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(scheduled_task_runs)").fetchall()
    }
    if "missed_count" not in run_columns:
        # No backfill: the column is not derivable after the fact,
        # and inventing a zero for history would claim those runs
        # were preceded by nothing missed.  Older runs simply
        # record nothing, which is what we actually know.
        conn.execute(
            "ALTER TABLE scheduled_task_runs ADD COLUMN "
            "missed_count INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_to_8(conn: sqlite3.Connection) -> None:
    # The table itself is created by the DDL above (IF NOT EXISTS),
    # which is what an existing database picks up on reopen.  The
    # version bump records that signals exist; there is nothing to
    # backfill, because a repository of emissions that were never
    # made is empty, not zero-filled.
    pass


def _migrate_to_9(conn: sqlite3.Connection) -> None:
    task_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(scheduled_tasks)").fetchall()
    }
    # Which workflow and which step a task was materialised from.
    # On the task rather than in the graph, because the graph is
    # edited as one document while the task is what actually runs;
    # a stale graph must not be able to lose track of a live task.
    # Defaults to empty, which is what every task that predates
    # workflows is -- a standalone task, not a member of anything.
    additions = {
        "workflow_id": "TEXT NOT NULL DEFAULT ''",
        "step_key": "TEXT NOT NULL DEFAULT ''",
    }
    for name, declaration in additions.items():
        if name not in task_columns:
            conn.execute(f"ALTER TABLE scheduled_tasks ADD COLUMN {name} {declaration}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_workflow "
        "ON scheduled_tasks(workflow_id, step_key)"
    )


def _migrate_to_10(conn: sqlite3.Connection) -> None:
    # A join used to remember only *which* upstreams had reported --
    # which is all it needs to decide when to run, and exactly what
    # it needs to lose their results.  The payloads are kept now, so
    # a step with several upstreams is told about all of them
    # instead of only whichever one happened to finish last.
    # Existing rows start empty, which reads as "this arrival
    # carried nothing", the honest answer for a round that was
    # recorded before there was anywhere to put it.
    join_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(signal_joins)").fetchall()
    }
    if "arrivals_json" not in join_columns:
        conn.execute(
            "ALTER TABLE signal_joins ADD COLUMN "
            "arrivals_json TEXT NOT NULL DEFAULT '{}'"
        )


def _migrate_to_11(conn: sqlite3.Connection) -> None:
    # What the work had to be true for, and what was concluded.
    #
    # No backfill, and the empty defaults are the correct reading
    # of every existing row rather than a placeholder: a task that
    # never declared an acceptance criterion has not been judged,
    # which is a different statement from having been judged and
    # passed.  Backfilling a pass would invent a verdict nobody
    # ever reached.
    task_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(scheduled_tasks)").fetchall()
    }
    if "acceptance_json" not in task_columns:
        conn.execute(
            "ALTER TABLE scheduled_tasks ADD COLUMN "
            "acceptance_json TEXT NOT NULL DEFAULT ''"
        )
    run_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(scheduled_task_runs)").fetchall()
    }
    for name, declaration in (
        ("verdict", "TEXT NOT NULL DEFAULT ''"),
        ("verification_json", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in run_columns:
            conn.execute(
                f"ALTER TABLE scheduled_task_runs ADD COLUMN {name} {declaration}"
            )


def _migrate_to_12(conn: sqlite3.Connection) -> None:
    # The words that asked for the task, so a row can say why it
    # exists long after the conversation is gone.
    #
    # No backfill, for the same reason the acceptance criterion
    # above has none: a task created before this column existed was
    # created without anyone having to produce the evidence, so
    # there is no quote to put there.  Writing one from the task's
    # own name would look exactly like the real thing and would be
    # a lie about who asked.
    task_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(scheduled_tasks)").fetchall()
    }
    if "request_quote" not in task_columns:
        conn.execute(
            "ALTER TABLE scheduled_tasks ADD COLUMN "
            "request_quote TEXT NOT NULL DEFAULT ''"
        )
    # A workflow is asked for by a person too, and its steps are
    # asked for only through it -- so the sentence that put the
    # chain there is stored once, on the chain, and copied onto
    # each step's task.  Reading a step row then answers "why does
    # this exist" with the words that started the whole thing,
    # rather than with the name of the step above it.
    workflow_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(workflows)").fetchall()
    }
    if "request_quote" not in workflow_columns:
        conn.execute(
            "ALTER TABLE workflows ADD COLUMN request_quote TEXT NOT NULL DEFAULT ''"
        )


def _migrate_to_13(conn: sqlite3.Connection) -> None:
    # What a task promises to leave behind, and what a run actually
    # left.  Two columns because they are two different statements:
    # the first is the contract and is edited with the task, the
    # second is an observation about one execution and is never
    # edited by anybody.
    #
    # No backfill, for the reason the columns before these have
    # none.  A task written before this existed was written without
    # anyone having to say what it produces, so there is nothing to
    # put there -- and inventing a path from the task's name would
    # look exactly like a declaration, and be a promise the task
    # never made.
    task_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(scheduled_tasks)").fetchall()
    }
    if "produces_json" not in task_columns:
        conn.execute(
            "ALTER TABLE scheduled_tasks ADD COLUMN "
            "produces_json TEXT NOT NULL DEFAULT '[]'"
        )
    run_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(scheduled_task_runs)").fetchall()
    }
    if "products_json" not in run_columns:
        conn.execute(
            "ALTER TABLE scheduled_task_runs ADD COLUMN "
            "products_json TEXT NOT NULL DEFAULT '[]'"
        )


def _migrate_to_14(conn: sqlite3.Connection) -> None:
    run_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(scheduled_task_runs)").fetchall()
    }
    if "workflow_run_id" not in run_columns:
        conn.execute(
            "ALTER TABLE scheduled_task_runs ADD COLUMN "
            "workflow_run_id TEXT NOT NULL DEFAULT ''"
        )
    workflow_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(workflows)").fetchall()
    }
    if "version" not in workflow_columns:
        conn.execute(
            "ALTER TABLE workflows ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
        )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS workflow_runs (
            id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            definition_version INTEGER NOT NULL,
            definition_snapshot_json TEXT NOT NULL,
            trigger_source TEXT NOT NULL,
            root_task_id TEXT NOT NULL,
            root_run_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finished_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_workflow_runs_workflow
            ON workflow_runs(workflow_id, created_at);
        CREATE TABLE IF NOT EXISTS workflow_signal_joins (
            task_id TEXT NOT NULL,
            workflow_run_id TEXT NOT NULL,
            satisfied_json TEXT NOT NULL DEFAULT '[]',
            arrivals_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(task_id, workflow_run_id)
        );
        """
    )


def _migrate_to_15(conn: sqlite3.Connection) -> None:
    """A task remembers the session that asked for it.

    ``request_quote`` holds the user's *words* -- that is what it is for, and
    grepping them for a session id was never going to work. The patrol needs the
    real thing: "which session owns this task" is how an unacknowledged failure
    becomes a proposal about a conversation. A column rather than a lookup, because
    the answer is a fact about creation and cannot be recovered later.
    """
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(scheduled_tasks)").fetchall()
    }
    if "session_id" not in columns:
        conn.execute(
            "ALTER TABLE scheduled_tasks ADD COLUMN "
            "session_id TEXT NOT NULL DEFAULT ''"
        )

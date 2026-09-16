from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Callable, Optional, TypeVar

from agent import shared
from .models import (
    ATTENTION_STATUSES,
    DEFAULT_SIGNAL_MAX_DEPTH,
    RUN_SKIPPED_STATUS,
    RUN_SUCCESS_STATUS,
    SIGNAL_MODE_ALL,
    TERMINAL_RUN_STATUSES,
    Acceptance,
    ClaimedTask,
    DeliveryTarget,
    NewScheduledTask,
    ScheduledTask,
    SignalEmission,
    TaskRun,
    TriggerSpec,
    Workflow,
    WorkflowStep,
    decode_verification,
    encode_verification,
    execution_snapshot,
    parse_task_signal,
    signal_mode,
    signal_names,
    step_trigger_spec,
    subscribes_to,
    task_signal_name,
    validate_acceptance,
    validate_workflow_graph,
    workflow_downstream_steps,
    workflow_step_order,
)


UTC = timezone.utc


def _new_id() -> str:
    return shared._new_id()


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _output_byte_count(path: str) -> int:
    """How big a run's output file is, or 0 if it cannot be measured.

    Carried in the report so a reader can tell a paragraph from a report
    before deciding whether to fetch it.  A pointer with no size is a pointer
    you have to follow in order to evaluate, which is the cost the pointer
    existed to avoid.
    """
    try:
        return int(Path(path).expanduser().stat().st_size)
    except OSError:
        return 0


def _run_report_payload(
    *,
    task_id: str,
    task_name: str,
    run_id: str,
    status: str,
    workflow_id: str = "",
    step_key: str = "",
    summary: str = "",
    output_path: str = "",
) -> dict[str, Any]:
    """One run's report, in the shape every reader of it expects.

    Two things produce this: the emission a run makes when it finishes, and
    the graph, read at the moment a run is claimed.  They have to agree.  A
    step told one thing when a signal woke it and another when somebody
    started it by hand is a step whose behaviour depends on how it started,
    which is exactly the distinction the handoff is supposed to erase.
    """
    payload: dict[str, Any] = {
        "task_id": task_id,
        "task_name": task_name,
        "run_id": run_id,
        "status": status,
    }
    if workflow_id:
        payload["workflow_id"] = workflow_id
    if step_key:
        payload["step_key"] = step_key
    if summary:
        # Long enough to be useful in a notification, short enough that a
        # chatty task cannot turn every downstream run's record into a copy of
        # its own output.
        payload["summary"] = summary[:500]
    if str(output_path or ""):
        payload["output_path"] = str(output_path)
        size = _output_byte_count(str(output_path))
        if size:
            payload["output_bytes"] = size
    return payload


_F = TypeVar("_F", bound=Callable)


def _synchronized(method: _F) -> _F:
    """Serialize a store method against the shared SQLite connection.

    One ``SchedulerStore`` is reached from several threads: the scheduler
    loop, channel workers, and — since synchronous tools moved off the event
    loop — any thread in the sync-tool pool.  ``check_same_thread=False``
    only silences sqlite3's ownership check; it does not make a connection
    safe to share, because a multi-statement transaction on one thread would
    interleave with statements issued from another.  Holding the lock for a
    whole call keeps each operation atomic.
    """

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


def _dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class SchedulerStore:
    SCHEMA_VERSION = 12

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or shared.SCHEDULER_DB_FILE
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()

    @_synchronized
    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _immediate_transaction(self):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    def _ensure_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    trigger_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    delivery_mode TEXT NOT NULL,
                    delivery_target_json TEXT NOT NULL,
                    model_override TEXT,
                    overlap_policy TEXT NOT NULL,
                    missed_run_policy TEXT NOT NULL,
                    workspace_root TEXT NOT NULL DEFAULT '',
                    context_policy TEXT NOT NULL DEFAULT 'stateless',
                    timeout_seconds INTEGER NOT NULL DEFAULT 1800,
                    retry_policy_json TEXT NOT NULL DEFAULT '{"max_attempts": 1, "backoff_seconds": 30}',
                    selected_skills_json TEXT NOT NULL DEFAULT '[]',
                    permission_profile TEXT NOT NULL DEFAULT 'inherit',
                    acceptance_json TEXT NOT NULL DEFAULT '',
                    request_quote TEXT NOT NULL DEFAULT '',
                    next_run_at TEXT,
                    lease_until TEXT,
                    active_run_id TEXT,
                    last_run_at TEXT,
                    last_success_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due
                    ON scheduled_tasks(enabled, next_run_at, lease_until);
                CREATE TABLE IF NOT EXISTS scheduled_task_runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    output_path TEXT NOT NULL DEFAULT '',
                    delivery_status TEXT NOT NULL DEFAULT '',
                    verdict TEXT NOT NULL DEFAULT '',
                    verification_json TEXT NOT NULL DEFAULT '',
                    config_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    trigger_source TEXT NOT NULL DEFAULT 'schedule',
                    attempt INTEGER NOT NULL DEFAULT 1,
                    cancel_requested_at TEXT,
                    retry_of_run_id TEXT NOT NULL DEFAULT '',
                    acknowledged_at TEXT,
                    missed_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES scheduled_tasks(id)
                );
                CREATE INDEX IF NOT EXISTS idx_scheduled_task_runs_task_id
                    ON scheduled_task_runs(task_id, created_at);
                CREATE TABLE IF NOT EXISTS signal_emissions (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    source TEXT NOT NULL DEFAULT 'manual',
                    depth INTEGER NOT NULL DEFAULT 0,
                    origin_id TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'pending',
                    reason TEXT NOT NULL DEFAULT '',
                    delivered_run_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    delivered_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_signal_emissions_pending
                    ON signal_emissions(state, created_at);
                CREATE INDEX IF NOT EXISTS idx_signal_emissions_name
                    ON signal_emissions(name, created_at);
                CREATE TABLE IF NOT EXISTS signal_deliveries (
                    id TEXT PRIMARY KEY,
                    emission_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL DEFAULT '',
                    outcome TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_signal_deliveries_emission
                    ON signal_deliveries(emission_id, created_at);
                CREATE TABLE IF NOT EXISTS signal_joins (
                    task_id TEXT PRIMARY KEY,
                    satisfied_json TEXT NOT NULL DEFAULT '[]',
                    arrivals_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflows (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL,
                    graph_json TEXT NOT NULL DEFAULT '{"steps": []}',
                    request_quote TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            current_version = int(
                self._conn.execute("PRAGMA user_version").fetchone()[0]
            )
            if current_version < self.SCHEMA_VERSION:
                self._migrate_schema(current_version, self.SCHEMA_VERSION)

    def _migrate_schema(self, current_version: int, target_version: int) -> None:
        version = int(current_version)
        while version < target_version:
            version += 1
            if version == 1:
                self._conn.execute("PRAGMA user_version = 1")
            elif version == 2:
                task_columns = {
                    row[1] for row in self._conn.execute(
                        "PRAGMA table_info(scheduled_tasks)"
                    ).fetchall()
                }
                run_columns = {
                    row[1] for row in self._conn.execute(
                        "PRAGMA table_info(scheduled_task_runs)"
                    ).fetchall()
                }
                additions = {
                    "workspace_root": "TEXT NOT NULL DEFAULT ''",
                    "context_policy": "TEXT NOT NULL DEFAULT 'stateless'",
                    "timeout_seconds": "INTEGER NOT NULL DEFAULT 1800",
                    "retry_policy_json": (
                        "TEXT NOT NULL DEFAULT "
                        "'{\"max_attempts\": 1, \"backoff_seconds\": 30}'"
                    ),
                }
                for name, declaration in additions.items():
                    if name not in task_columns:
                        self._conn.execute(
                            f"ALTER TABLE scheduled_tasks ADD COLUMN {name} {declaration}"
                        )
                run_additions = {
                    "config_snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
                    "trigger_source": "TEXT NOT NULL DEFAULT 'schedule'",
                    "attempt": "INTEGER NOT NULL DEFAULT 1",
                    "cancel_requested_at": "TEXT",
                }
                for name, declaration in run_additions.items():
                    if name not in run_columns:
                        self._conn.execute(
                            f"ALTER TABLE scheduled_task_runs ADD COLUMN {name} {declaration}"
                        )
                self._conn.execute("PRAGMA user_version = 2")
            elif version == 3:
                run_columns = {
                    row[1] for row in self._conn.execute(
                        "PRAGMA table_info(scheduled_task_runs)"
                    ).fetchall()
                }
                if "retry_of_run_id" not in run_columns:
                    self._conn.execute(
                        "ALTER TABLE scheduled_task_runs ADD COLUMN "
                        "retry_of_run_id TEXT NOT NULL DEFAULT ''"
                    )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_scheduled_task_runs_queued "
                    "ON scheduled_task_runs(status, started_at)"
                )
                self._conn.execute("PRAGMA user_version = 3")
            elif version == 4:
                task_columns = {
                    row[1] for row in self._conn.execute(
                        "PRAGMA table_info(scheduled_tasks)"
                    ).fetchall()
                }
                if "selected_skills_json" not in task_columns:
                    self._conn.execute(
                        "ALTER TABLE scheduled_tasks ADD COLUMN "
                        "selected_skills_json TEXT NOT NULL DEFAULT '[]'"
                    )
                self._conn.execute("PRAGMA user_version = 4")
            elif version == 5:
                task_columns = {
                    row[1] for row in self._conn.execute(
                        "PRAGMA table_info(scheduled_tasks)"
                    ).fetchall()
                }
                if "permission_profile" not in task_columns:
                    self._conn.execute(
                        "ALTER TABLE scheduled_tasks ADD COLUMN "
                        "permission_profile TEXT NOT NULL DEFAULT 'inherit'"
                    )
                self._conn.execute("PRAGMA user_version = 5")
            elif version == 6:
                run_columns = {
                    row[1] for row in self._conn.execute(
                        "PRAGMA table_info(scheduled_task_runs)"
                    ).fetchall()
                }
                if "acknowledged_at" not in run_columns:
                    self._conn.execute(
                        "ALTER TABLE scheduled_task_runs ADD COLUMN "
                        "acknowledged_at TEXT"
                    )
                    # Runs that predate this column were already reported
                    # through the run list, which is how the user found out
                    # about them.  Backfilling finished runs as seen keeps the
                    # new counter from announcing a backlog of history on the
                    # first launch after the upgrade.
                    self._conn.execute(
                        "UPDATE scheduled_task_runs SET acknowledged_at = finished_at "
                        "WHERE finished_at IS NOT NULL"
                    )
                self._conn.execute("PRAGMA user_version = 6")
            elif version == 7:
                run_columns = {
                    row[1] for row in self._conn.execute(
                        "PRAGMA table_info(scheduled_task_runs)"
                    ).fetchall()
                }
                if "missed_count" not in run_columns:
                    # No backfill: the column is not derivable after the fact,
                    # and inventing a zero for history would claim those runs
                    # were preceded by nothing missed.  Older runs simply
                    # record nothing, which is what we actually know.
                    self._conn.execute(
                        "ALTER TABLE scheduled_task_runs ADD COLUMN "
                        "missed_count INTEGER NOT NULL DEFAULT 0"
                    )
                self._conn.execute("PRAGMA user_version = 7")
            elif version == 8:
                # The table itself is created by the DDL above (IF NOT EXISTS),
                # which is what an existing database picks up on reopen.  The
                # version bump records that signals exist; there is nothing to
                # backfill, because a repository of emissions that were never
                # made is empty, not zero-filled.
                self._conn.execute("PRAGMA user_version = 8")
            elif version == 9:
                task_columns = {
                    row[1] for row in self._conn.execute(
                        "PRAGMA table_info(scheduled_tasks)"
                    ).fetchall()
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
                        self._conn.execute(
                            f"ALTER TABLE scheduled_tasks ADD COLUMN {name} {declaration}"
                        )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_workflow "
                    "ON scheduled_tasks(workflow_id, step_key)"
                )
                # signal_joins and workflows are created by the DDL above;
                # nothing to backfill for either.  A join's satisfied set starts
                # empty by definition, and a workflow nobody has authored is
                # absent rather than an empty row.
                self._conn.execute("PRAGMA user_version = 9")
            elif version == 10:
                # A join used to remember only *which* upstreams had reported --
                # which is all it needs to decide when to run, and exactly what
                # it needs to lose their results.  The payloads are kept now, so
                # a step with several upstreams is told about all of them
                # instead of only whichever one happened to finish last.
                # Existing rows start empty, which reads as "this arrival
                # carried nothing", the honest answer for a round that was
                # recorded before there was anywhere to put it.
                join_columns = {
                    row[1] for row in self._conn.execute(
                        "PRAGMA table_info(signal_joins)"
                    ).fetchall()
                }
                if "arrivals_json" not in join_columns:
                    self._conn.execute(
                        "ALTER TABLE signal_joins ADD COLUMN "
                        "arrivals_json TEXT NOT NULL DEFAULT '{}'"
                    )
                self._conn.execute("PRAGMA user_version = 10")
            elif version == 11:
                # What the work had to be true for, and what was concluded.
                #
                # No backfill, and the empty defaults are the correct reading
                # of every existing row rather than a placeholder: a task that
                # never declared an acceptance criterion has not been judged,
                # which is a different statement from having been judged and
                # passed.  Backfilling a pass would invent a verdict nobody
                # ever reached.
                task_columns = {
                    row[1] for row in self._conn.execute(
                        "PRAGMA table_info(scheduled_tasks)"
                    ).fetchall()
                }
                if "acceptance_json" not in task_columns:
                    self._conn.execute(
                        "ALTER TABLE scheduled_tasks ADD COLUMN "
                        "acceptance_json TEXT NOT NULL DEFAULT ''"
                    )
                run_columns = {
                    row[1] for row in self._conn.execute(
                        "PRAGMA table_info(scheduled_task_runs)"
                    ).fetchall()
                }
                for name, declaration in (
                    ("verdict", "TEXT NOT NULL DEFAULT ''"),
                    ("verification_json", "TEXT NOT NULL DEFAULT ''"),
                ):
                    if name not in run_columns:
                        self._conn.execute(
                            f"ALTER TABLE scheduled_task_runs ADD COLUMN "
                            f"{name} {declaration}"
                        )
                self._conn.execute("PRAGMA user_version = 11")
            elif version == 12:
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
                    row[1] for row in self._conn.execute(
                        "PRAGMA table_info(scheduled_tasks)"
                    ).fetchall()
                }
                if "request_quote" not in task_columns:
                    self._conn.execute(
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
                    row[1] for row in self._conn.execute(
                        "PRAGMA table_info(workflows)"
                    ).fetchall()
                }
                if "request_quote" not in workflow_columns:
                    self._conn.execute(
                        "ALTER TABLE workflows ADD COLUMN "
                        "request_quote TEXT NOT NULL DEFAULT ''"
                    )
                self._conn.execute("PRAGMA user_version = 12")

    def _task_from_row(self, row: sqlite3.Row) -> ScheduledTask:
        return ScheduledTask(
            id=row["id"],
            name=row["name"],
            kind=row["kind"],
            enabled=bool(row["enabled"]),
            trigger=TriggerSpec.from_json(row["trigger_json"]),
            payload=json.loads(row["payload_json"]),
            delivery_mode=row["delivery_mode"],
            delivery_target=DeliveryTarget.from_json(row["delivery_target_json"]),
            model_override=row["model_override"],
            overlap_policy=row["overlap_policy"],
            missed_run_policy=row["missed_run_policy"],
            workspace_root=row["workspace_root"],
            context_policy=row["context_policy"],
            timeout_seconds=int(row["timeout_seconds"]),
            retry_policy=json.loads(row["retry_policy_json"]),
            selected_skills=json.loads(row["selected_skills_json"]),
            permission_profile=row["permission_profile"],
            next_run_at=_dt(row["next_run_at"]),
            lease_until=_dt(row["lease_until"]),
            active_run_id=row["active_run_id"],
            last_run_at=_dt(row["last_run_at"]),
            last_success_at=_dt(row["last_success_at"]),
            created_at=_dt(row["created_at"]) or datetime.now(UTC),
            updated_at=_dt(row["updated_at"]) or datetime.now(UTC),
            acceptance=Acceptance.from_json(row["acceptance_json"]),
            workflow_id=row["workflow_id"] or "",
            step_key=row["step_key"] or "",
            request_quote=row["request_quote"] or "",
        )

    def _run_from_row(self, row: sqlite3.Row) -> TaskRun:
        return TaskRun(
            id=row["id"],
            task_id=row["task_id"],
            scheduled_for=_dt(row["scheduled_for"]) or datetime.now(UTC),
            started_at=_dt(row["started_at"]) or datetime.now(UTC),
            finished_at=_dt(row["finished_at"]),
            status=row["status"],
            summary=row["summary"],
            error=row["error"],
            output_path=row["output_path"],
            delivery_status=row["delivery_status"],
            verdict=row["verdict"] or "",
            verification=decode_verification(row["verification_json"]),
            config_snapshot=json.loads(row["config_snapshot_json"]),
            trigger_source=row["trigger_source"],
            attempt=int(row["attempt"]),
            cancel_requested_at=_dt(row["cancel_requested_at"]),
            retry_of_run_id=row["retry_of_run_id"],
            acknowledged_at=_dt(row["acknowledged_at"]),
            missed_count=int(row["missed_count"] or 0),
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    def _check_acceptance(self, task: NewScheduledTask) -> None:
        """Refuse an acceptance criterion this task could never be judged by.

        Called on every write of a task definition, which is the only moment
        the check can be refused while somebody is still looking at the screen.
        The workspace is the task's own; the output root is the same one a run's
        verifier will be given, so a command that passes here is not rejected
        later for being pointed at the wrong directory.
        """
        validate_acceptance(
            getattr(task, "acceptance", None),
            workspace_root=str(getattr(task, "workspace_root", "") or ""),
            output_dir=str(shared.DEFAULT_OUTPUT_DIR),
            label=f"任务「{getattr(task, 'name', '')}」",
        )

    @_synchronized
    def create_task(
        self, task: NewScheduledTask, now: Optional[datetime] = None
    ) -> ScheduledTask:
        self._check_acceptance(task)
        created_at = (now or datetime.now(UTC)).astimezone(UTC)
        task_id = _new_id()
        next_run_at = task.trigger.initial_run_at(created_at)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO scheduled_tasks (
                    id, name, kind, enabled, trigger_json, payload_json,
                    delivery_mode, delivery_target_json, model_override,
                    overlap_policy, missed_run_policy, workspace_root,
                    context_policy, timeout_seconds, retry_policy_json,
                    selected_skills_json,
                    permission_profile,
                    acceptance_json,
                    next_run_at, lease_until,
                    active_run_id, last_run_at, last_success_at, created_at, updated_at,
                    workflow_id, step_key, request_quote
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?)
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
                    task.overlap_policy,
                    task.missed_run_policy,
                    task.workspace_root,
                    task.context_policy,
                    int(task.timeout_seconds),
                    json.dumps(task.retry_policy, ensure_ascii=False),
                    json.dumps(task.selected_skills, ensure_ascii=False),
                    task.permission_profile,
                    task.acceptance.to_json(),
                    _iso(next_run_at),
                    _iso(created_at),
                    _iso(created_at),
                    task.workflow_id,
                    task.step_key,
                    str(getattr(task, "request_quote", "") or ""),
                ),
            )
        created = self.get_task(task_id)
        assert created is not None
        return created

    @_synchronized
    def find_matching_task(self, task: NewScheduledTask) -> Optional[ScheduledTask]:
        row = self._conn.execute(
            """
            SELECT * FROM scheduled_tasks
            WHERE name = ?
              AND kind = ?
              AND enabled = ?
              AND trigger_json = ?
              AND payload_json = ?
              AND delivery_mode = ?
              AND delivery_target_json = ?
              AND (
                    (model_override IS NULL AND ? IS NULL)
                    OR model_override = ?
                  )
              AND overlap_policy = ?
              AND missed_run_policy = ?
              AND workspace_root = ?
              AND context_policy = ?
              AND timeout_seconds = ?
              AND retry_policy_json = ?
              AND selected_skills_json = ?
              AND permission_profile = ?
              AND acceptance_json = ?
              AND workflow_id = ?
              AND step_key = ?
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (
                task.name,
                task.kind,
                1 if task.enabled else 0,
                task.trigger.to_json(),
                json.dumps(task.payload, ensure_ascii=False),
                task.delivery_mode,
                task.delivery_target.to_json(),
                task.model_override,
                task.model_override,
                task.overlap_policy,
                task.missed_run_policy,
                task.workspace_root,
                task.context_policy,
                int(task.timeout_seconds),
                json.dumps(task.retry_policy, ensure_ascii=False),
                json.dumps(task.selected_skills, ensure_ascii=False),
                task.permission_profile,
                # Part of the identity, not a decoration on it.  Two tasks that
                # disagree about what "done" means are two different tasks, and
                # treating them as one would answer "make this run only when
                # the tests pass" with the existing task that runs regardless
                # -- reporting success while the criterion the caller just
                # stated is silently discarded.
                task.acceptance.to_json(),
                task.workflow_id,
                task.step_key,
            ),
        ).fetchone()
        return self._task_from_row(row) if row else None

    @_synchronized
    def disable_duplicate_enabled_tasks(
        self, now: Optional[datetime] = None
    ) -> int:
        rows = self._conn.execute(
            """
            SELECT * FROM scheduled_tasks
            WHERE enabled = 1
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()
        seen: set[tuple[object, ...]] = set()
        duplicate_ids: list[str] = []
        for row in rows:
            signature = (
                row["name"],
                row["kind"],
                row["trigger_json"],
                row["payload_json"],
                row["delivery_mode"],
                row["delivery_target_json"],
                row["model_override"],
                row["overlap_policy"],
                row["missed_run_policy"],
                row["workspace_root"],
                row["context_policy"],
                row["timeout_seconds"],
                row["retry_policy_json"],
                row["selected_skills_json"],
                row["permission_profile"],
                row["workflow_id"],
                row["step_key"],
            )
            if signature in seen:
                duplicate_ids.append(row["id"])
            else:
                seen.add(signature)
        if not duplicate_ids:
            return 0
        updated_at = _iso((now or datetime.now(UTC)).astimezone(UTC))
        with self._conn:
            self._conn.executemany(
                """
                UPDATE scheduled_tasks
                SET enabled = 0, updated_at = ?
                WHERE id = ?
                """,
                [(updated_at, task_id) for task_id in duplicate_ids],
            )
        return len(duplicate_ids)

    @_synchronized
    def list_tasks(self) -> list[ScheduledTask]:
        rows = self._conn.execute(
            "SELECT * FROM scheduled_tasks ORDER BY created_at ASC"
        ).fetchall()
        return [self._task_from_row(row) for row in rows]

    @_synchronized
    def get_task(self, task_id: str) -> Optional[ScheduledTask]:
        row = self._conn.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ? LIMIT 1",
            (task_id,),
        ).fetchone()
        return self._task_from_row(row) if row else None

    @_synchronized
    def list_runs(self, task_id: str) -> list[TaskRun]:
        rows = self._conn.execute(
            """
            SELECT * FROM scheduled_task_runs
            WHERE task_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (task_id,),
        ).fetchall()
        return [self._run_from_row(row) for row in rows]

    @_synchronized
    def get_run(self, task_id: str, run_id: str) -> Optional[TaskRun]:
        row = self._conn.execute(
            """
            SELECT * FROM scheduled_task_runs
            WHERE task_id = ? AND id = ?
            LIMIT 1
            """,
            (task_id, run_id),
        ).fetchone()
        return self._run_from_row(row) if row else None

    # ── Signals: what happened, and who was waiting for it ───────────────
    #
    # Emissions are written down before anything is done about them.  The
    # tempting shortcut -- emit by directly queueing a run for each subscriber
    # -- has no way to be honest: if the process stops between "somebody
    # emitted" and "the runs are queued", the signal is simply gone, and the
    # only evidence was in memory.  Recording first and delivering second costs
    # one table and buys the ability to say what did *not* happen, which is the
    # same trade the run record already makes.
    #
    # The other half of the design is that delivery is bounded.  Subscriptions
    # are a graph nobody draws, so a loop can assemble itself out of two
    # unrelated edits; every emission therefore carries its distance from the
    # start of the cascade, and delivery stops at a ceiling rather than trying
    # to prove the graph acyclic.

    def _emission_from_row(self, row: sqlite3.Row) -> SignalEmission:
        return SignalEmission(
            id=row["id"],
            name=row["name"],
            payload=json.loads(row["payload_json"] or "{}"),
            source=row["source"],
            depth=int(row["depth"]),
            origin_id=row["origin_id"],
            state=row["state"],
            reason=row["reason"],
            delivered_run_id=row["delivered_run_id"],
            created_at=_dt(row["created_at"]),
            delivered_at=_dt(row["delivered_at"]),
        )

    @_synchronized
    def emit_signal(
        self,
        name: str,
        payload: Optional[dict[str, Any]] = None,
        *,
        source: str = "manual",
        depth: int = 0,
        origin_id: str = "",
        now: Optional[datetime] = None,
    ) -> SignalEmission:
        """Record that a signal happened.  Delivery is a separate step.

        Returns the stored emission, whose id is the handle for everything
        downstream: the runs it produces point back at it, which is how a
        cascade can be read end to end instead of guessed at from timestamps.
        """
        created = (now or datetime.now(UTC)).astimezone(UTC)
        with self._conn:
            return self._insert_emission(
                name,
                payload,
                source=source,
                depth=depth,
                origin_id=origin_id,
                now=created,
            )

    def _insert_emission(
        self,
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
        self._conn.execute(
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

    @_synchronized
    def get_emission(self, emission_id: str) -> Optional[SignalEmission]:
        row = self._conn.execute(
            "SELECT * FROM signal_emissions WHERE id = ?", (emission_id,)
        ).fetchone()
        return self._emission_from_row(row) if row else None

    @_synchronized
    def list_emissions(
        self, name: Optional[str] = None, limit: int = 50
    ) -> list[SignalEmission]:
        """Recent emissions, newest first -- the audit trail for signals."""
        params: list[Any] = []
        clause = ""
        if name:
            clause = "WHERE name = ?"
            params.append(str(name).strip())
        params.append(max(1, int(limit)))
        rows = self._conn.execute(
            f"""
            SELECT * FROM signal_emissions
            {clause}
            ORDER BY created_at DESC, rowid DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [self._emission_from_row(row) for row in rows]

    @_synchronized
    def signal_names(self, limit: int = 100) -> list[dict[str, Any]]:
        """Every signal name that has ever been emitted, newest first.

        Exists so a subscription can be *picked* rather than typed.  The name
        is matched exactly and a near-miss never fires, so an interface that
        only offered a text box would be inviting the one failure this design
        cannot detect on its own.
        """
        rows = self._conn.execute(
            """
            SELECT name, COUNT(*) AS total, MAX(created_at) AS last_at
            FROM signal_emissions
            GROUP BY name
            ORDER BY last_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        return [
            {"name": row["name"], "count": int(row["total"]), "last_at": row["last_at"]}
            for row in rows
        ]

    @_synchronized
    def describe_signal_problem(self, name: str) -> str:
        """Why this subscription could never fire, or ``""`` if it might.

        Refuses only what can be *proven* wrong, which for a signal is less
        than it sounds.  A free-form name like ``report.ready`` cannot be
        checked here -- nothing but the emitter knows whether it is spelled
        right -- so it is accepted and left to the ``unmatched`` state, which
        records the miss instead of hiding it.

        A task signal is different: it names a task and a status, and both can
        be looked up.  Checking them turns the two mistakes people actually
        make -- a mistyped task id, and a status no run can reach -- into an
        error at the moment of writing, which is the only time the person
        still has the context to fix it.
        """
        text = str(name or "").strip()
        if not text:
            return "信号名称不能为空"
        parsed = parse_task_signal(text)
        if parsed is None:
            return ""
        task_id, status = parsed
        if status not in TERMINAL_RUN_STATUSES:
            return (
                f"运行不会以「{status}」结束；可用的状态："
                + "、".join(TERMINAL_RUN_STATUSES)
            )
        if self.get_task(task_id) is None:
            return f"找不到 id 为 {task_id} 的任务"
        return ""

    def _signal_subscribers(self, name: str) -> list[ScheduledTask]:
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
        rows = self._conn.execute(
            "SELECT * FROM scheduled_tasks WHERE enabled = 1"
        ).fetchall()
        for row in rows:
            task = self._task_from_row(row)
            if subscribes_to(task.trigger, name):
                subscribers.append(task)
        return subscribers

    def _satisfied_joins(self, task_id: str) -> set[str]:
        row = self._conn.execute(
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

    def _advance_join(
        self, task: ScheduledTask, emission: SignalEmission, now: datetime
    ) -> list[str]:
        """Record one arrival at a join.  Returns the names still missing.

        An empty list means this arrival completed the round.  The satisfied
        set means "upstreams heard from since the last completed round", and
        the caller spends it the moment the round completes -- that is, as
        soon as this returns an empty list, whether or not a run follows.
        Spending it on completion rather than on queueing is what stops a
        stale arrival from closing a second round later: if a completed round
        that coalesced into a run already in flight left its set behind, then
        the next report from any one upstream would find the set still full,
        complete a round nobody opened, and run the step on evidence that had
        already been acted on.

        Because it is a set, a fast upstream succeeding three times while a
        slow one is still working contributes one arrival, not three, so the
        downstream step does not run repeatedly for one turn of the crank.

        The arrival's *payload* is kept alongside its name, because knowing
        that three upstreams reported is not the same as being able to tell the
        step below what any of them produced.  Keyed by signal name, so the
        same "one arrival per round" rule applies to the content as to the
        name: the last payload from a name is the one that describes the round.

        Caller owns the transaction.
        """
        required = set(signal_names(task.trigger))
        satisfied = self._satisfied_joins(task.id)
        satisfied.add(emission.name)
        # Intersected with what this join actually waits for, so a name that
        # this task is not subscribed to can never contribute to completing it.
        satisfied &= required
        missing = sorted(required - satisfied)
        arrivals = self._join_arrivals(task.id)
        if emission.name in required:
            arrivals[emission.name] = dict(emission.payload)
        self._conn.execute(
            """
            INSERT INTO signal_joins (
                task_id, satisfied_json, arrivals_json, updated_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                satisfied_json = excluded.satisfied_json,
                arrivals_json = excluded.arrivals_json,
                updated_at = excluded.updated_at
            """,
            (
                task.id,
                json.dumps(sorted(satisfied), ensure_ascii=False),
                json.dumps(arrivals, ensure_ascii=False),
                _iso(now),
            ),
        )
        return missing

    def _join_arrivals(self, task_id: str) -> dict[str, dict[str, Any]]:
        """The payloads a join has collected since its last completed round."""
        row = self._conn.execute(
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

    def join_progress(self, task_id: str) -> dict[str, Any]:
        """What a join has heard from so far, so the interface can show it.

        Without this, a join that is one name short looks exactly like a task
        that is broken -- nothing has run and nothing says why.
        """
        row = self._conn.execute(
            "SELECT trigger_json FROM scheduled_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        required: list[str] = []
        if row is not None:
            trigger = TriggerSpec.from_json(row["trigger_json"])
            if signal_mode(trigger) == SIGNAL_MODE_ALL:
                required = signal_names(trigger)
        satisfied = self._satisfied_joins(task_id)
        return {
            "required": required,
            "satisfied": sorted(satisfied),
            "missing": [name for name in required if name not in satisfied],
        }

    def _take_join(self, task_id: str) -> dict[str, dict[str, Any]]:
        """Read a join's collected arrivals, then spend them.  Caller owns the txn.

        Read-and-delete in one call because the two are never wanted apart: an
        arrival that is read but not spent would let a stale payload describe a
        later round, and one spent without being read is exactly the loss this
        column exists to fix.
        """
        arrivals = self._join_arrivals(task_id)
        self._conn.execute("DELETE FROM signal_joins WHERE task_id = ?", (task_id,))
        return arrivals

    def _record_delivery(
        self,
        emission_id: str,
        task_id: str,
        outcome: str,
        *,
        run_id: str = "",
        reason: str = "",
        now: datetime,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO signal_deliveries (
                id, emission_id, task_id, run_id, outcome, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (_new_id(), emission_id, task_id, run_id, outcome, reason, _iso(now)),
        )

    def _pending_run_for(self, task_id: str) -> Optional[str]:
        """The run already waiting for, or running, this task, if any."""
        row = self._conn.execute(
            """
            SELECT id FROM scheduled_task_runs
            WHERE task_id = ? AND status IN ('queued', 'running')
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        return row["id"] if row else None

    def _enqueue_signal_run_in_transaction(
        self,
        task: ScheduledTask,
        emission: SignalEmission,
        now: datetime,
        arrivals: Optional[dict[str, dict[str, Any]]] = None,
    ) -> str:
        """Queue one run for a subscriber, carrying the emission it answers.

        The snapshot records where this run came from, because the run that
        follows a signal is the only place that knows it: when *it* finishes
        and emits in turn, the depth and the cascade id have to travel with it
        or the ceiling has nothing to count.

        A join's accumulated arrivals are *not* spent here: the delivery loop
        spends them when the round completes, which it must do whether or not
        this method runs, and hands them in as *arrivals*.  See
        :meth:`_advance_join` and :meth:`_take_join`.
        """
        run_id = _new_id()
        snapshot = execution_snapshot(task)
        snapshot["signal"] = {
            "name": emission.name,
            "emission_id": emission.id,
            "origin_id": emission.origin_id,
            "depth": emission.depth,
            "payload": emission.payload,
            "source": emission.source,
        }
        # ``signal`` is the emission that *triggered* this run, and the depth
        # is inherited from it.  ``signals`` is every upstream whose report
        # this round is made of -- all of them for a join, the same single one
        # for an ordinary subscriber.  Two keys rather than one list because
        # they answer different questions, and collapsing them would make "who
        # woke this run" and "whose results it has" the same answer, which is
        # only true when there is exactly one upstream.
        snapshot["signals"] = (
            [
                {"name": name, "payload": dict(payload)}
                for name, payload in sorted(arrivals.items())
            ]
            if arrivals
            else [{"name": emission.name, "payload": dict(emission.payload)}]
        )
        self._conn.execute(
            """
            INSERT INTO scheduled_task_runs (
                id, task_id, scheduled_for, started_at, finished_at, status,
                summary, error, output_path, delivery_status,
                config_snapshot_json, trigger_source, attempt,
                missed_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, 'queued', '', '', '', '', ?,
                      ?, 1, 0, ?, ?)
            """,
            (
                run_id,
                task.id,
                _iso(now),
                _iso(now),
                json.dumps(snapshot, ensure_ascii=False),
                f"signal:{emission.name}",
                _iso(now),
                _iso(now),
            ),
        )
        return run_id

    @_synchronized
    def deliver_signals(
        self,
        now: Optional[datetime] = None,
        *,
        limit: int = 50,
        max_depth: int = DEFAULT_SIGNAL_MAX_DEPTH,
    ) -> dict[str, int]:
        """Turn waiting emissions into runs.  Returns a tally *by emission*.

        One emission has exactly one outcome, even when it reached several
        subscribers by different routes -- delivered to one, folded into
        another's existing run -- so the tally counts emissions and each
        emission settles into the state it was counted under.  The per-pair
        detail lives in :meth:`signal_deliveries`.

        Idempotent by state: an emission leaves ``pending`` exactly once,
        inside the same transaction that queues its runs, so a crash midway
        cannot double-deliver -- it either committed both or neither.

        A join (``mode="all"``) is the one case where an emission can be fully
        accounted for and still start nothing: it arrived, it was recorded, and
        the step it belongs to is waiting for its siblings.  That is
        ``waiting``, and it is its own state because calling it ``delivered``
        would claim a run that does not exist.
        """
        current = (now or datetime.now(UTC)).astimezone(UTC)
        tally = {
            "delivered": 0,
            "coalesced": 0,
            "waiting": 0,
            "refused": 0,
            "unmatched": 0,
        }
        with self._immediate_transaction():
            rows = self._conn.execute(
                """
                SELECT * FROM signal_emissions
                WHERE state = 'pending'
                ORDER BY created_at ASC, rowid ASC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            for row in rows:
                emission = self._emission_from_row(row)
                subscribers = self._signal_subscribers(emission.name)
                if not subscribers:
                    self._settle_emission(
                        emission,
                        state="unmatched",
                        reason="没有任务订阅这个信号",
                        now=current,
                    )
                    tally["unmatched"] += 1
                    continue
                if emission.depth > int(max_depth):
                    # Refused for every subscriber: the cascade has gone as far
                    # as it is allowed to, whatever shape it has.
                    for task in subscribers:
                        self._record_delivery(
                            emission.id,
                            task.id,
                            "refused",
                            reason=f"级联深度超过上限 {int(max_depth)}",
                            now=current,
                        )
                    self._settle_emission(
                        emission,
                        state="refused",
                        reason=f"级联深度超过上限 {int(max_depth)}",
                        now=current,
                    )
                    tally["refused"] += 1
                    continue
                first_run_id = ""
                delivered = coalesced = waiting = 0
                waiting_on: list[str] = []
                for task in subscribers:
                    arrivals: dict[str, dict[str, Any]] = {}
                    if signal_mode(task.trigger) == SIGNAL_MODE_ALL:
                        # A join records this arrival and runs only when the
                        # last one shows up.  Landing here without a run is the
                        # normal case, not a failure, so it says so plainly
                        # instead of being filed under a state that means
                        # something else.  Recorded before the pending check
                        # below, because an arrival during a run belongs to the
                        # next round rather than to that run.
                        missing = self._advance_join(task, emission, current)
                        if missing:
                            self._record_delivery(
                                emission.id,
                                task.id,
                                "waiting",
                                reason=(
                                    "该任务在等所有上游信号，本次到达已记录；"
                                    f"还在等：{'、'.join(missing)}"
                                ),
                                now=current,
                            )
                            waiting += 1
                            waiting_on.extend(missing)
                            continue
                        # The round completed with this arrival, so it is spent
                        # here, before the run question below decides what it
                        # turns into.  A pending run is not a reason to keep
                        # the set: the round is answered either way -- by a new
                        # run or by the one already in flight -- and holding it
                        # would let a stale arrival close a round that was
                        # never opened.  ``test_the_next_round_starts_from_
                        # nothing`` guards the rule.
                        #
                        # Taken rather than cleared, because the payloads are
                        # the round's content and the run below is the last
                        # chance to hand them on.  A round that coalesces into
                        # an in-flight run drops them, which is what the
                        # ``coalesced`` reason below already says.
                        arrivals = self._take_join(task.id)
                    pending = self._pending_run_for(task.id)
                    if pending:
                        # Already something to do.  For a signal that repeats
                        # -- "data.ready" while the report is still being
                        # written -- one pending run is the useful answer, and
                        # queueing another would let a fast emitter stack up
                        # work nobody asked for.
                        #
                        # But the reason says only what is true.  The run it is
                        # recorded against was built from an earlier emission,
                        # so this signal's payload does not reach it: the
                        # signal is accounted for, not passed on.  Saying
                        # "merged in" would be the kind of comfortable wording
                        # this whole record exists to replace.
                        self._record_delivery(
                            emission.id,
                            task.id,
                            "coalesced",
                            run_id=pending,
                            reason=(
                                "该任务已有待执行或执行中的运行，本次信号不再另起一次运行；"
                                "信号本身已记录，但它的内容不会送达那次运行"
                            ),
                            now=current,
                        )
                        coalesced += 1
                        continue
                    run_id = self._enqueue_signal_run_in_transaction(
                        task, emission, current, arrivals
                    )
                    self._record_delivery(
                        emission.id, task.id, "delivered", run_id=run_id, now=current
                    )
                    first_run_id = first_run_id or run_id
                    delivered += 1
                if delivered:
                    self._settle_emission(
                        emission,
                        state="delivered",
                        reason="",
                        run_id=first_run_id,
                        now=current,
                    )
                    tally["delivered"] += 1
                elif waiting:
                    self._settle_emission(
                        emission,
                        state="waiting",
                        reason=(
                            f"{waiting} 个订阅任务在等其余上游信号，"
                            f"还在等：{'、'.join(dict.fromkeys(waiting_on))}"
                        ),
                        now=current,
                    )
                    tally["waiting"] += 1
                else:
                    self._settle_emission(
                        emission,
                        state="coalesced",
                        reason=(
                            f"{coalesced} 个订阅任务都已有待执行或执行中的运行，"
                            "本次信号没有另起运行"
                        ),
                        now=current,
                    )
                    tally["coalesced"] += 1
        return tally

    def _settle_emission(
        self,
        emission: SignalEmission,
        *,
        state: str,
        reason: str,
        now: datetime,
        run_id: str = "",
    ) -> None:
        self._conn.execute(
            """
            UPDATE signal_emissions
            SET state = ?, reason = ?, delivered_run_id = ?, delivered_at = ?
            WHERE id = ? AND state = 'pending'
            """,
            (state, reason, run_id, _iso(now), emission.id),
        )

    @_synchronized
    def signal_deliveries(self, emission_id: str) -> list[dict[str, Any]]:
        """Per-subscriber outcomes for one emission.

        One emission can be delivered to one task while being folded into a
        run that another task had already queued, so the outcome belongs to the
        pair, not to the emission -- a single state would have to pick one and
        lie about the rest.
        """
        rows = self._conn.execute(
            """
            SELECT * FROM signal_deliveries
            WHERE emission_id = ?
            ORDER BY created_at ASC, rowid ASC
            """,
            (emission_id,),
        ).fetchall()
        return [
            {
                "task_id": row["task_id"],
                "run_id": row["run_id"],
                "outcome": row["outcome"],
                "reason": row["reason"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def _emit_run_signal(
        self,
        task_id: str,
        run_id: str,
        status: str,
        finished_at: datetime,
        summary: str,
        output_path: str = "",
    ) -> None:
        """Announce that a run reached ``status``.  Caller owns the transaction.

        Called from :meth:`complete_run` rather than from the runtime, which is
        the point: the runtime has several ways to end a run, and every one of
        them has to remember to announce it.  Recording the emission where the
        status is recorded removes that class of forgetting -- a status that
        exists is a signal that exists.

        The payload carries a *pointer* to the run's output, not the output.
        ``summary`` is a first line capped at 120 characters by the caller, so
        it is a notification rather than a payload; anything a downstream step
        actually needs to read lives at ``output_path``.  Handing over the
        address costs a few dozen bytes and is what makes "the previous step's
        output" reachable at all -- without it a subscriber knows a step
        succeeded but has no way to say where its work went.

        The size of that file travels with the address, so a reader can tell a
        paragraph from a report before deciding whether to go and read it.

        ``workflow_id`` and ``step_key`` travel with it for the same reason:
        they are the names a downstream step can use, and the ids it cannot
        guess.

        Depth is inherited from the emission this run answered, so a run that
        was itself started by a signal emits one step further from the root.
        A run a clock or a person started has no parent and emits at depth zero,
        which is what makes it the readable start of a cascade.
        """
        row = self._conn.execute(
            "SELECT config_snapshot_json FROM scheduled_task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        snapshot: dict[str, Any] = {}
        if row is not None:
            try:
                decoded = json.loads(row["config_snapshot_json"] or "{}")
            except (TypeError, ValueError):
                decoded = {}
            if isinstance(decoded, dict):
                snapshot = decoded
        parent = snapshot.get("signal")
        parent = parent if isinstance(parent, dict) else {}
        task_row = self._conn.execute(
            "SELECT name, workflow_id, step_key FROM scheduled_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        payload = _run_report_payload(
            task_id=task_id,
            task_name=str(task_row["name"]) if task_row is not None else "",
            run_id=run_id,
            status=status,
            workflow_id=(
                str(task_row["workflow_id"] or "") if task_row is not None else ""
            ),
            step_key=str(task_row["step_key"] or "") if task_row is not None else "",
            summary=summary,
            output_path=str(output_path or ""),
        )
        self._insert_emission(
            task_signal_name(task_id, status),
            payload,
            source="task",
            depth=int(parent.get("depth", 0) or 0) + 1 if parent else 0,
            origin_id=str(parent.get("origin_id", "") or "") if parent else "",
            now=finished_at,
        )

    # ── Attention: runs nobody has been told about ───────────────────────
    #
    # A scheduled run happens when nobody is watching, so "the run list shows
    # it" only helps someone who already suspected something went wrong.  The
    # marker below is what turns a silent failure into something the interface
    # can insist on.  It lives in the database rather than in a push
    # notification because the common case is that no client is connected: a
    # notification that is only delivered to whoever happens to be looking is
    # not a notification.

    @staticmethod
    def _attention_clause() -> tuple[str, list[Any]]:
        """Runs a person should look at.

        Two ways in.  The run ended badly, or it arrived carrying the news
        that earlier occurrences were skipped while nothing was running.  The
        second is not a failure of *this* run -- it succeeded -- but it is the
        one case where waiting to be read does not work: the run that resumes
        the schedule reports success, so nothing else in the system will ever
        mention the gap, and a daily report can be missing for a week with
        every run marked fine.

        The status list is read from :data:`ATTENTION_STATUSES` rather than
        written out here, which is what keeps this query and
        :func:`run_needs_attention` -- the same rule in Python -- from
        disagreeing when a status is added.  ``unverified`` arrived that way.
        """
        placeholders = ", ".join("?" for _ in ATTENTION_STATUSES)
        return (
            f"(status IN ({placeholders}) OR missed_count > 0)",
            list(ATTENTION_STATUSES),
        )

    @_synchronized
    def unacknowledged_attention_counts(self) -> dict[str, int]:
        """Task id -> number of runs nobody has looked at yet."""
        clause, params = self._attention_clause()
        rows = self._conn.execute(
            f"""
            SELECT task_id, COUNT(*) AS total
            FROM scheduled_task_runs
            WHERE {clause} AND acknowledged_at IS NULL
            GROUP BY task_id
            """,
            params,
        ).fetchall()
        return {row["task_id"]: int(row["total"]) for row in rows}

    @_synchronized
    def acknowledge_run(
        self, task_id: str, run_id: str, now: Optional[datetime] = None
    ) -> bool:
        """Mark one run as seen.  False when there was nothing to see."""
        clause, params = self._attention_clause()
        stamp = _iso((now or datetime.now(UTC)).astimezone(UTC))
        with self._conn:
            cursor = self._conn.execute(
                f"""
                UPDATE scheduled_task_runs
                SET acknowledged_at = ?, updated_at = ?
                WHERE task_id = ? AND id = ?
                  AND acknowledged_at IS NULL
                  AND {clause}
                """,
                [stamp, stamp, task_id, run_id, *params],
            )
        return cursor.rowcount == 1

    @_synchronized
    def acknowledge_attention(
        self, task_id: Optional[str] = None, now: Optional[datetime] = None
    ) -> int:
        """Mark every unseen run as seen, for one task or for all."""
        clause, params = self._attention_clause()
        stamp = _iso((now or datetime.now(UTC)).astimezone(UTC))
        scope = "task_id = ? AND " if task_id else ""
        scope_params = [task_id] if task_id else []
        with self._conn:
            cursor = self._conn.execute(
                f"""
                UPDATE scheduled_task_runs
                SET acknowledged_at = ?, updated_at = ?
                WHERE acknowledged_at IS NULL AND {scope}{clause}
                """,
                [stamp, stamp, *scope_params, *params],
            )
        return int(cursor.rowcount)

    @_synchronized
    def latest_run(self, task_id: str) -> Optional[TaskRun]:
        row = self._conn.execute(
            """
            SELECT * FROM scheduled_task_runs
            WHERE task_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        return self._run_from_row(row) if row else None

    @_synchronized
    def update_task(
        self,
        task_id: str,
        task: NewScheduledTask,
        *,
        now: Optional[datetime] = None,
    ) -> Optional[ScheduledTask]:
        self._check_acceptance(task)
        updated_at = (now or datetime.now(UTC)).astimezone(UTC)
        next_run_at = task.trigger.initial_run_at(updated_at)
        with self._conn:
            cursor = self._conn.execute(
                """
                UPDATE scheduled_tasks
                SET name = ?, kind = ?, enabled = ?, trigger_json = ?,
                    payload_json = ?, delivery_mode = ?, delivery_target_json = ?,
                    model_override = ?, overlap_policy = ?, missed_run_policy = ?,
                    workspace_root = ?, context_policy = ?, timeout_seconds = ?,
                    retry_policy_json = ?, next_run_at = ?, updated_at = ?
                    , selected_skills_json = ?, permission_profile = ?
                    , acceptance_json = ?
                    , workflow_id = ?, step_key = ?
                WHERE id = ?
                """,
                (
                    task.name,
                    task.kind,
                    1 if task.enabled else 0,
                    task.trigger.to_json(),
                    json.dumps(task.payload, ensure_ascii=False),
                    task.delivery_mode,
                    task.delivery_target.to_json(),
                    task.model_override,
                    task.overlap_policy,
                    task.missed_run_policy,
                    task.workspace_root,
                    task.context_policy,
                    int(task.timeout_seconds),
                    json.dumps(task.retry_policy, ensure_ascii=False),
                    _iso(next_run_at),
                    _iso(updated_at),
                    json.dumps(task.selected_skills, ensure_ascii=False),
                    task.permission_profile,
                    task.acceptance.to_json(),
                    task.workflow_id,
                    task.step_key,
                    task_id,
                ),
            )
        return self.get_task(task_id) if cursor.rowcount else None

    def _upstream_reports_in_transaction(
        self, task: ScheduledTask
    ) -> dict[str, dict[str, Any]]:
        """What the runs this task waits for last produced, read off its trigger.

        A run used to be told about its upstreams only by the emission that
        woke it, which made the handoff a property of *how the run started*
        rather than of *what the step needs*.  Starting a step by hand ("run
        now"), or retrying it with the latest configuration, produced a run
        that looked like any other and had been told nothing about the work it
        depends on -- while the trigger already names those upstreams, so the
        answer was in hand and simply unread.

        Keyed by the signal name the subscription waits for, which makes these
        arrivals indistinguishable from the ones a real emission would have
        left: the same dict reaches ``_describe_upstream_results`` either way.

        The trigger is read rather than the workflow's ``depends_on`` because
        the trigger is what the task actually waits on, and it exists for
        hand-wired signal chains that have no workflow at all.
        """
        arrivals: dict[str, dict[str, Any]] = {}
        for name in signal_names(task.trigger):
            parsed = parse_task_signal(name)
            if parsed is None:
                # A free-form name like ``report.ready``.  Nothing in this
                # database can vouch for what it produced, so it is not a
                # report and has no address to hand over.
                continue
            upstream_id, status = parsed
            upstream = self._conn.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ?", (upstream_id,)
            ).fetchone()
            if upstream is None:
                continue
            latest = self._conn.execute(
                """
                SELECT * FROM scheduled_task_runs
                WHERE task_id = ? AND status = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (upstream_id, status),
            ).fetchone()
            if latest is None:
                continue
            arrivals[name] = _run_report_payload(
                task_id=upstream_id,
                task_name=str(upstream["name"] or ""),
                run_id=str(latest["id"]),
                status=str(latest["status"]),
                workflow_id=str(upstream["workflow_id"] or ""),
                step_key=str(upstream["step_key"] or ""),
                summary=str(latest["summary"] or ""),
                output_path=str(latest["output_path"] or ""),
            )
        return arrivals

    def _claim_task_in_transaction(
        self,
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
            arrivals = self._upstream_reports_in_transaction(task)
            if arrivals:
                snapshot["signals"] = [
                    {"name": name, "payload": payload}
                    for name, payload in sorted(arrivals.items())
                ]
        self._conn.execute(
            """
            INSERT INTO scheduled_task_runs (
                id, task_id, scheduled_for, started_at, finished_at, status,
                summary, error, output_path, delivery_status,
                config_snapshot_json, trigger_source, attempt,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, 'running', '', '', '', '', ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                task.id,
                _iso(scheduled_for),
                _iso(now),
                json.dumps(snapshot, ensure_ascii=False),
                trigger_source,
                max(1, int(attempt)),
                _iso(now),
                _iso(now),
            ),
        )
        cursor = self._conn.execute(
            """
            UPDATE scheduled_tasks
            SET lease_until = ?, active_run_id = ?, updated_at = ?
            WHERE id = ? AND active_run_id IS NULL
            """,
            (_iso(lease_until), run_id, _iso(now), task.id),
        )
        if cursor.rowcount != 1:
            self._conn.execute(
                "DELETE FROM scheduled_task_runs WHERE id = ?", (run_id,)
            )
            return None
        refreshed = self._conn.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ?", (task.id,)
        ).fetchone()
        claimed_task = self._task_from_row(refreshed) if refreshed else task
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
                created_at=now,
                updated_at=now,
            ),
        )

    @_synchronized
    def claim_task_now(
        self,
        task_id: str,
        *,
        now: Optional[datetime] = None,
        lease_seconds: int = 300,
    ) -> Optional[ClaimedTask]:
        if lease_seconds < 3:
            raise ValueError("lease_seconds must be at least 3")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        with self._immediate_transaction():
            self._recover_stale_runs_in_transaction(current)
            row = self._conn.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ? AND active_run_id IS NULL",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            task = self._task_from_row(row)
            return self._claim_task_in_transaction(
                task,
                now=current,
                lease_seconds=lease_seconds,
                scheduled_for=current,
                trigger_source="manual",
                snapshot=execution_snapshot(task),
            )

    @_synchronized
    def claim_retry(
        self,
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
        with self._immediate_transaction():
            self._recover_stale_runs_in_transaction(current)
            task_row = self._conn.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ? AND active_run_id IS NULL",
                (task_id,),
            ).fetchone()
            source_row = self._conn.execute(
                """
                SELECT * FROM scheduled_task_runs
                WHERE id = ? AND task_id = ? AND status <> 'running'
                """,
                (run_id, task_id),
            ).fetchone()
            if task_row is None or source_row is None:
                return None
            task = self._task_from_row(task_row)
            source = self._run_from_row(source_row)
            snapshot = execution_snapshot(task) if use_latest else source.config_snapshot
            if not snapshot:
                snapshot = execution_snapshot(task)
            return self._claim_task_in_transaction(
                task,
                now=current,
                lease_seconds=lease_seconds,
                scheduled_for=source.scheduled_for,
                trigger_source="retry_latest" if use_latest else "retry_snapshot",
                snapshot=snapshot,
                attempt=source.attempt + 1,
            )

    @_synchronized
    def request_cancel(
        self, task_id: str, run_id: str, *, now: Optional[datetime] = None
    ) -> bool:
        requested_at = (now or datetime.now(UTC)).astimezone(UTC)
        with self._conn:
            cursor = self._conn.execute(
                """
                UPDATE scheduled_task_runs
                SET cancel_requested_at = ?, updated_at = ?
                WHERE id = ? AND task_id = ? AND status = 'running'
                """,
                (_iso(requested_at), _iso(requested_at), run_id, task_id),
            )
        return cursor.rowcount == 1

    @_synchronized
    def enqueue_retry(
        self,
        task_id: str,
        source_run_id: str,
        *,
        retry_at: datetime,
    ) -> Optional[TaskRun]:
        retry_at = retry_at.astimezone(UTC)
        with self._immediate_transaction():
            source_row = self._conn.execute(
                """
                SELECT * FROM scheduled_task_runs
                WHERE id = ? AND task_id = ? AND status = 'failed'
                """,
                (source_run_id, task_id),
            ).fetchone()
            if source_row is None:
                return None
            existing = self._conn.execute(
                """
                SELECT * FROM scheduled_task_runs
                WHERE task_id = ? AND retry_of_run_id = ? AND status IN ('queued', 'running')
                LIMIT 1
                """,
                (task_id, source_run_id),
            ).fetchone()
            if existing is not None:
                return self._run_from_row(existing)
            source = self._run_from_row(source_row)
            run_id = _new_id()
            self._conn.execute(
                """
                INSERT INTO scheduled_task_runs (
                    id, task_id, scheduled_for, started_at, finished_at, status,
                    summary, error, output_path, delivery_status,
                    config_snapshot_json, trigger_source, attempt,
                    cancel_requested_at, retry_of_run_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, 'queued', '', '', '', '', ?,
                          'automatic_retry', ?, NULL, ?, ?, ?)
                """,
                (
                    run_id,
                    task_id,
                    _iso(source.scheduled_for),
                    _iso(retry_at),
                    json.dumps(source.config_snapshot, ensure_ascii=False),
                    source.attempt + 1,
                    source_run_id,
                    _iso(datetime.now(UTC)),
                    _iso(datetime.now(UTC)),
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM scheduled_task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return self._run_from_row(row) if row else None

    @_synchronized
    def claim_due_tasks(
        self, now: datetime, limit: int = 10, lease_seconds: int = 300
    ) -> list[ClaimedTask]:
        if lease_seconds < 3:
            raise ValueError("lease_seconds must be at least 3")
        now = now.astimezone(UTC)
        claimed: list[ClaimedTask] = []
        with self._immediate_transaction():
            self._recover_stale_runs_in_transaction(now)
            queued_rows = self._conn.execute(
                """
                SELECT r.* FROM scheduled_task_runs r
                JOIN scheduled_tasks t ON t.id = r.task_id
                WHERE r.status = 'queued'
                  AND r.started_at <= ?
                  AND t.enabled = 1
                  AND t.active_run_id IS NULL
                ORDER BY r.started_at ASC, r.created_at ASC
                LIMIT ?
                """,
                (_iso(now), int(limit)),
            ).fetchall()
            for queued_row in queued_rows:
                run = self._run_from_row(queued_row)
                task_row = self._conn.execute(
                    "SELECT * FROM scheduled_tasks WHERE id = ?", (run.task_id,)
                ).fetchone()
                if task_row is None:
                    continue
                task = self._task_from_row(task_row)
                lease_until = now + timedelta(seconds=lease_seconds)
                task_cursor = self._conn.execute(
                    """
                    UPDATE scheduled_tasks
                    SET lease_until = ?, active_run_id = ?, updated_at = ?
                    WHERE id = ? AND enabled = 1 AND active_run_id IS NULL
                    """,
                    (_iso(lease_until), run.id, _iso(now), task.id),
                )
                if task_cursor.rowcount != 1:
                    continue
                self._conn.execute(
                    """
                    UPDATE scheduled_task_runs
                    SET status = 'running', started_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (_iso(now), _iso(now), run.id),
                )
                run.status = "running"
                run.started_at = now
                run.updated_at = now
                claimed.append(ClaimedTask(task=task, run=run))
            remaining = max(0, int(limit) - len(claimed))
            if remaining == 0:
                return claimed
            rows = self._conn.execute(
                """
                SELECT * FROM scheduled_tasks
                WHERE enabled = 1
                  AND next_run_at IS NOT NULL
                  AND next_run_at <= ?
                  AND active_run_id IS NULL
                ORDER BY next_run_at ASC, created_at ASC
                LIMIT ?
                """,
                (_iso(now), remaining),
            ).fetchall()
            for row in rows:
                task = self._task_from_row(row)
                if task.next_run_at is None:
                    continue
                run_id = _new_id()
                started_at = now
                scheduled_for = task.next_run_at
                next_run_at = task.trigger.advance_after_claim(scheduled_for, now)
                # Counted here because this is the only place the jump is
                # visible: `advance_after_claim` moves the cursor straight to
                # the future and the occurrences it stepped over leave no other
                # trace.  Doing it in the same transaction as the claim keeps
                # the number attached to the run that actually resumes the
                # schedule.
                missed_count = task.trigger.count_missed(scheduled_for, next_run_at)
                lease_until = now + timedelta(seconds=lease_seconds)
                self._conn.execute(
                    """
                    INSERT INTO scheduled_task_runs (
                        id, task_id, scheduled_for, started_at, finished_at, status,
                        summary, error, output_path, delivery_status,
                        config_snapshot_json, trigger_source, attempt,
                        missed_count, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, 'running', '', '', '', '', ?, 'schedule', 1, ?, ?, ?)
                    """,
                    (
                        run_id,
                        task.id,
                        _iso(task.next_run_at),
                        _iso(started_at),
                        json.dumps(execution_snapshot(task), ensure_ascii=False),
                        int(missed_count),
                        _iso(started_at),
                        _iso(started_at),
                    ),
                )
                cursor = self._conn.execute(
                    """
                    UPDATE scheduled_tasks
                    SET next_run_at = ?, lease_until = ?, active_run_id = ?, updated_at = ?
                    WHERE id = ?
                      AND enabled = 1
                      AND active_run_id IS NULL
                      AND next_run_at <= ?
                    """,
                    (
                        _iso(next_run_at),
                        _iso(lease_until),
                        run_id,
                        _iso(now),
                        task.id,
                        _iso(now),
                    ),
                )
                if cursor.rowcount != 1:
                    self._conn.execute(
                        "DELETE FROM scheduled_task_runs WHERE id = ?", (run_id,)
                    )
                    continue
                refreshed_row = self._conn.execute(
                    "SELECT * FROM scheduled_tasks WHERE id = ?", (task.id,)
                ).fetchone()
                refreshed = self._task_from_row(refreshed_row) if refreshed_row else None
                assert refreshed is not None
                # Built by hand rather than re-read, so every field the row got
                # has to be repeated here: one that is left out silently reads
                # as its default while the database holds the real value, and
                # the run the scheduler is about to execute would disagree with
                # the run the history shows.
                claimed.append(
                    ClaimedTask(
                        task=refreshed,
                        run=TaskRun(
                            id=run_id,
                            task_id=task.id,
                            scheduled_for=task.next_run_at,
                            started_at=started_at,
                            finished_at=None,
                            status="running",
                            config_snapshot=execution_snapshot(task),
                            trigger_source="schedule",
                            missed_count=missed_count,
                            created_at=started_at,
                            updated_at=started_at,
                        ),
                    )
                )
        return claimed

    @_synchronized
    def recover_stale_runs(self, now: datetime) -> int:
        now = now.astimezone(UTC)
        with self._immediate_transaction():
            return self._recover_stale_runs_in_transaction(now)

    def _recover_stale_runs_in_transaction(self, now: datetime) -> int:
        rows = self._conn.execute(
            """
            SELECT t.id AS task_id, t.active_run_id, r.scheduled_for
            FROM scheduled_tasks t
            JOIN scheduled_task_runs r ON r.id = t.active_run_id
            WHERE t.active_run_id IS NOT NULL
              AND t.lease_until IS NOT NULL
              AND t.lease_until < ?
              AND r.status = 'running'
            """,
            (_iso(now),),
        ).fetchall()
        recovered = 0
        for row in rows:
            cursor = self._conn.execute(
                """
                UPDATE scheduled_tasks
                SET next_run_at = ?, lease_until = NULL, active_run_id = NULL, updated_at = ?
                WHERE id = ? AND active_run_id = ? AND lease_until < ?
                """,
                (
                    row["scheduled_for"],
                    _iso(now),
                    row["task_id"],
                    row["active_run_id"],
                    _iso(now),
                ),
            )
            if cursor.rowcount != 1:
                continue
            self._conn.execute(
                    """
                    UPDATE scheduled_task_runs
                    SET status = 'interrupted', finished_at = ?, updated_at = ?
                    WHERE id = ? AND task_id = ? AND status = 'running'
                    """,
                (
                    _iso(now),
                    _iso(now),
                    row["active_run_id"],
                    row["task_id"],
                ),
            )
            recovered += 1
        return recovered

    @_synchronized
    def renew_lease(
        self,
        task_id: str,
        run_id: str,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        if lease_seconds < 3:
            raise ValueError("lease_seconds must be at least 3")
        now = now.astimezone(UTC)
        with self._conn:
            cursor = self._conn.execute(
                """
                UPDATE scheduled_tasks
                SET lease_until = ?, updated_at = ?
                WHERE id = ?
                  AND active_run_id = ?
                  AND lease_until IS NOT NULL
                  AND lease_until >= ?
                  AND EXISTS (
                      SELECT 1 FROM scheduled_task_runs r
                      WHERE r.id = ? AND r.task_id = scheduled_tasks.id
                        AND r.status = 'running'
                  )
                """,
                (
                    _iso(now + timedelta(seconds=lease_seconds)),
                    _iso(now),
                    task_id,
                    run_id,
                    _iso(now),
                    run_id,
                ),
            )
        return cursor.rowcount == 1

    @_synchronized
    def release_claim(
        self,
        task_id: str,
        run_id: str,
        *,
        now: datetime,
        reason: str,
    ) -> bool:
        """Release an unexecuted claim without consuming its scheduled occurrence."""
        now = now.astimezone(UTC)
        with self._immediate_transaction():
            row = self._conn.execute(
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
            task_cursor = self._conn.execute(
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
            self._conn.execute(
                """
                UPDATE scheduled_task_runs
                SET status = 'interrupted', error = ?, finished_at = ?, updated_at = ?
                WHERE id = ? AND task_id = ? AND status = 'running'
                """,
                (reason, _iso(now), _iso(now), run_id, task_id),
            )
        return True

    @_synchronized
    def owns_unexpired_lease(
        self, task_id: str, run_id: str, *, now: datetime
    ) -> bool:
        now = now.astimezone(UTC)
        row = self._conn.execute(
            """
            SELECT 1
            FROM scheduled_tasks t
            JOIN scheduled_task_runs r
              ON r.id = t.active_run_id AND r.task_id = t.id
            WHERE t.id = ?
              AND t.active_run_id = ?
              AND t.lease_until IS NOT NULL
              AND t.lease_until >= ?
              AND r.status = 'running'
            LIMIT 1
            """,
            (task_id, run_id, _iso(now)),
        ).fetchone()
        return row is not None

    def _skip_blocked_steps_in_transaction(
        self,
        workflow_id: str,
        step_key: str,
        status: str,
        finished_at: datetime,
    ) -> list[str]:
        """Record the steps this failure blocks as skipped.  Caller owns the txn.

        This is the one thing a chain needs that a lone task never did.  A step
        runs when all of its upstreams succeeded, so a step whose upstream
        failed waits for a signal that is no longer coming -- for ever, and in
        silence.  Nothing would look wrong: no run, no error, a task that
        appears simply not to have come up yet.

        The whole subtree is written at once rather than one level at a time,
        because the graph already knows it: each blocked step emits its own
        ``skipped`` signal, and its own blocked steps are recorded here too, so
        the recursion cannot stop halfway.  Its signals are still emitted, so a
        task outside the workflow that subscribed to "this step was skipped"
        still hears about it.

        A step that already has a run queued or running is left alone.  That
        run belongs to an earlier round and was legitimately asked for;
        cancelling it to satisfy this skip would throw away work somebody
        wanted, and it will announce its own outcome when it finishes.
        """
        if status == RUN_SUCCESS_STATUS:
            return []
        workflow = self.get_workflow(workflow_id)
        if workflow is None:
            return []
        blocked_keys = workflow_downstream_steps(workflow.steps, step_key)
        if not blocked_keys:
            return []
        reason = (
            f"上游步骤「{step_key}」以 {status} 结束，这一步等不到它的成功信号，"
            "因此没有运行"
        )
        skipped: list[str] = []
        for step in workflow.steps:
            key = str(step.key).strip()
            if key not in blocked_keys:
                continue
            row = self._conn.execute(
                """
                SELECT id FROM scheduled_tasks
                WHERE workflow_id = ? AND step_key = ?
                LIMIT 1
                """,
                (workflow_id, key),
            ).fetchone()
            if row is None:
                continue
            blocked_task_id = row["id"]
            if self._pending_run_for(blocked_task_id):
                continue
            run_id = _new_id()
            snapshot = {
                "workflow": {
                    "workflow_id": workflow_id,
                    "step_key": key,
                    "blocked_by_step": step_key,
                    "blocked_by_status": status,
                }
            }
            self._conn.execute(
                """
                INSERT INTO scheduled_task_runs (
                    id, task_id, scheduled_for, started_at, finished_at, status,
                    summary, error, output_path, delivery_status,
                    config_snapshot_json, trigger_source, attempt,
                    missed_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '', '', '', ?, ?, 1, 0, ?, ?)
                """,
                (
                    run_id,
                    blocked_task_id,
                    _iso(finished_at),
                    _iso(finished_at),
                    _iso(finished_at),
                    RUN_SKIPPED_STATUS,
                    reason,
                    json.dumps(snapshot, ensure_ascii=False),
                    f"workflow:{step_key}",
                    _iso(finished_at),
                    _iso(finished_at),
                ),
            )
            # ``last_run_at`` is deliberately not touched.  It answers "when did
            # this task last run", and a skipped task has never run -- its never
            # having run is the whole content of this record.  A step that looks
            # like it ran is the failure this row exists to prevent.
            self._emit_run_signal(
                blocked_task_id,
                run_id,
                RUN_SKIPPED_STATUS,
                finished_at,
                reason,
            )
            skipped.append(blocked_task_id)
        return skipped

    @_synchronized
    def complete_run(
        self,
        task_id: str,
        run_id: str,
        *,
        finished_at: datetime,
        status: str,
        summary: str = "",
        error: str = "",
        output_path: str = "",
        delivery_status: str = "",
        verdict: str = "",
        verification: Any = None,
    ) -> bool:
        finished_at = finished_at.astimezone(UTC)
        with self._immediate_transaction():
            task_cursor = self._conn.execute(
                """
                UPDATE scheduled_tasks
                SET active_run_id = NULL, lease_until = NULL,
                    last_run_at = ?,
                    last_success_at = CASE WHEN ? = 'succeeded' THEN ? ELSE last_success_at END,
                    updated_at = ?
                WHERE id = ?
                  AND active_run_id = ?
                  AND EXISTS (
                      SELECT 1 FROM scheduled_task_runs r
                      WHERE r.id = ? AND r.task_id = scheduled_tasks.id
                        AND r.status = 'running'
                  )
                """,
                (
                    _iso(finished_at),
                    status,
                    _iso(finished_at),
                    _iso(finished_at),
                    task_id,
                    run_id,
                    run_id,
                ),
            )
            if task_cursor.rowcount != 1:
                return False
            run_cursor = self._conn.execute(
                """
                UPDATE scheduled_task_runs
                SET status = ?, summary = ?, error = ?, output_path = ?,
                    delivery_status = ?, verdict = ?, verification_json = ?,
                    finished_at = ?, updated_at = ?
                WHERE id = ? AND task_id = ? AND status = 'running'
                """,
                (
                    status,
                    summary,
                    error,
                    output_path,
                    delivery_status,
                    verdict,
                    encode_verification(verification),
                    _iso(finished_at),
                    _iso(finished_at),
                    run_id,
                    task_id,
                ),
            )
            if run_cursor.rowcount != 1:
                raise RuntimeError("owned scheduler run disappeared during completion")
            # Inside the same transaction as the status, deliberately.  If the
            # two could commit separately, a crash between them would leave a
            # task that visibly succeeded while whatever was waiting on it
            # waits forever -- the exact failure that is hardest to notice.
            self._emit_run_signal(
                task_id, run_id, status, finished_at, summary, output_path
            )
            # Same reasoning, one step further: a step that did not do its work
            # cannot unblock the steps below it, and their run will never
            # happen.  Recorded here, with the status, so no control path can
            # finish a step without the steps it blocks being settled.
            membership = self._conn.execute(
                "SELECT workflow_id, step_key FROM scheduled_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if membership is not None and membership["workflow_id"]:
                self._skip_blocked_steps_in_transaction(
                    membership["workflow_id"],
                    str(membership["step_key"] or ""),
                    status,
                    finished_at,
                )
        return True

    def _live_workflow_step(self, task: ScheduledTask) -> bool:
        """True when *task* is a step whose workflow still exists.

        The guards that send a step's owner to the graph are all about a graph
        that can still be saved: deleting a step outright leaves the steps
        below it subscribed to a signal nobody emits any more, and the next
        save of the workflow builds a fresh task for it.  A workflow that has
        been deleted leaves its steps behind on purpose -- the record that
        they ran is their history -- and owns nothing any more: for those
        tasks every one of those reasons is gone, and a refusal that points
        at a workflow nobody can open is how a leftover becomes undeletable.
        """
        if not task.workflow_id:
            return False
        return self.get_workflow(task.workflow_id) is not None

    @_synchronized
    def set_enabled(self, task_id: str, enabled: bool) -> None:
        task = self.get_task(task_id)
        if task is not None and self._live_workflow_step(task):
            # A step's own switch is rewritten from its workflow's every time
            # that workflow is saved, so flipping it here would be a promise
            # the next save breaks.  Pausing the workflow is the switch that
            # holds.
            raise ValueError(
                f"「{task.name}」是流程中的步骤，请暂停整个流程，或到流程里删除该步骤"
            )
        now = datetime.now(UTC)
        with self._conn:
            self._conn.execute(
                """
                UPDATE scheduled_tasks
                SET enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (1 if enabled else 0, _iso(now), task_id),
            )

    @_synchronized
    def delete_task(self, task_id: str) -> None:
        task = self.get_task(task_id)
        if task is not None and task.active_run_id:
            # The run's completion writes back to the row this would remove,
            # and would fail with an "owned run disappeared" error in the
            # scheduler thread -- a cancellation the person asked for and can
            # see beats a crash they cannot.
            raise ValueError(f"「{task.name}」正在运行，请先取消运行")
        if task is not None and self._live_workflow_step(task):
            # The graph, not the task list, decides which steps exist.
            # Deleting a step on its own would leave the steps below it
            # subscribed to a signal nobody emits any more, and the next save
            # of the workflow would build a fresh task for the step -- so the
            # run history this deletion was meant to tidy up would reappear
            # under a new id.
            raise ValueError(
                f"「{task.name}」是流程中的步骤，请删除整个流程，或到流程里移除该步骤"
            )
        with self._conn:
            # Its deliveries go with it, for the same reason its runs do: they
            # are that task's history, and rows left behind would name a task
            # and a run that no longer exist -- a record that cannot be read
            # and cannot be cleaned up, since nothing else knows the id.
            self._conn.execute(
                "DELETE FROM signal_deliveries WHERE task_id = ?", (task_id,)
            )
            # Same reasoning for a half-satisfied join: it is this task's own
            # progress, and it is meaningless without the task.
            self._conn.execute(
                "DELETE FROM signal_joins WHERE task_id = ?", (task_id,)
            )
            self._conn.execute(
                "DELETE FROM scheduled_task_runs WHERE task_id = ?", (task_id,)
            )
            # The emissions themselves stay.  A signal is a record of something
            # that happened, and it may already have been delivered to other
            # tasks whose runs point back at it; deleting it would rewrite
            # their history to make this deletion tidier.
            self._conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))

    # -- workflows -------------------------------------------------------
    #
    # A workflow is a stored graph of steps.  The tasks that carry the steps
    # are created from it, not the other way round, so the graph is the thing
    # a person edits and the tasks are a consequence -- which is what makes
    # "add a step" a single action instead of a re-wiring of signals.
    #
    # Steps are matched to tasks by ``step_key``, and the task id is kept
    # across re-materialization.  That matters more than it looks: a task id
    # appears in the signal names its downstream steps subscribe to, so
    # recreating tasks on every edit would silently break every edge.

    def _workflow_from_row(self, row: sqlite3.Row) -> Workflow:
        return Workflow.from_graph(
            row["graph_json"],
            workflow_id=row["id"],
            request_quote=row["request_quote"] or "",
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    def _validate_graph(self, steps: list[WorkflowStep]) -> list[str]:
        """Check a graph, including every step's acceptance criterion.

        The folders are resolved first so each criterion is checked against the
        directory its step will actually run in.  Nothing is written before this
        returns, which is what keeps a refused graph from leaving a workflow row
        with no tasks behind it -- the state ``create_workflow`` exists to make
        impossible.
        """
        order = workflow_step_order(steps)
        workspaces, _ = self._step_workspaces(steps, order)
        return validate_workflow_graph(
            steps,
            acceptance_workspaces=workspaces,
            output_dir=str(shared.DEFAULT_OUTPUT_DIR),
        )

    @_synchronized
    def create_workflow(
        self, workflow: Workflow, *, now: Optional[datetime] = None
    ) -> Workflow:
        """Store a graph, then build the tasks it describes.

        The two happen together on purpose.  A graph with no tasks behind it
        is a drawing: it looks like a plan and runs nothing, and the person who
        made it has no way to tell the difference from the outside.
        """
        self._validate_graph(workflow.steps)
        created_at = (now or datetime.now(UTC)).astimezone(UTC)
        workflow_id = _new_id()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO workflows (
                    id, name, description, enabled, graph_json,
                    request_quote, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow_id,
                    workflow.name,
                    workflow.description,
                    1 if workflow.enabled else 0,
                    json.dumps(workflow.to_graph(), ensure_ascii=False),
                    str(getattr(workflow, "request_quote", "") or ""),
                    _iso(created_at),
                    _iso(created_at),
                ),
            )
        self.materialize_workflow(workflow_id, now=created_at)
        stored = self.get_workflow(workflow_id)
        assert stored is not None
        return stored

    @_synchronized
    def get_workflow(self, workflow_id: str) -> Optional[Workflow]:
        row = self._conn.execute(
            "SELECT * FROM workflows WHERE id = ? LIMIT 1", (workflow_id,)
        ).fetchone()
        return self._workflow_from_row(row) if row else None

    @_synchronized
    def list_workflows(self) -> list[Workflow]:
        rows = self._conn.execute(
            "SELECT * FROM workflows ORDER BY created_at ASC, id ASC"
        ).fetchall()
        return [self._workflow_from_row(row) for row in rows]

    @_synchronized
    def update_workflow(
        self,
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
        """
        if self.get_workflow(workflow_id) is None:
            return None
        self._validate_graph(workflow.steps)
        updated_at = (now or datetime.now(UTC)).astimezone(UTC)
        with self._conn:
            # `request_quote` is deliberately absent here.  It records who asked
            # for the chain, and editing a step does not change that -- while a
            # caller that rebuilds the graph from an edit form has no quote to
            # send, so writing the column from `workflow` would blank the one
            # piece of evidence the row holds about its own origin.
            self._conn.execute(
                """
                UPDATE workflows
                SET name = ?, description = ?, enabled = ?, graph_json = ?,
                    updated_at = ?
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
            self.materialize_workflow(workflow_id, now=updated_at)
        return self.get_workflow(workflow_id)

    @_synchronized
    def delete_workflow(
        self, workflow_id: str, *, now: Optional[datetime] = None
    ) -> list[str]:
        """Drop the graph and disable the tasks it built.  Returns their ids.

        The tasks are disabled rather than deleted, and their run history is
        left where it is.  Deleting them would take the record of what the
        workflow did with it, and a workflow is exactly the kind of thing
        somebody deletes in order to stop it -- not in order to forget that it
        ran.  Disabling also stops any subscription they hold from firing.
        """
        now_dt = (now or datetime.now(UTC)).astimezone(UTC)
        disabled = [task.id for task in self.step_tasks(workflow_id).values()]
        with self._conn:
            for task_id in disabled:
                self._conn.execute(
                    """
                    UPDATE scheduled_tasks
                    SET enabled = 0, next_run_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (_iso(now_dt), task_id),
                )
            self._conn.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
        return sorted(disabled)

    @_synchronized
    def step_tasks(self, workflow_id: str) -> dict[str, ScheduledTask]:
        """The tasks behind a workflow's steps, keyed by step key."""
        rows = self._conn.execute(
            """
            SELECT * FROM scheduled_tasks
            WHERE workflow_id = ? AND step_key != ''
            ORDER BY created_at ASC
            """,
            (workflow_id,),
        ).fetchall()
        return {row["step_key"]: self._task_from_row(row) for row in rows}

    def _step_workspaces(
        self, steps: list[WorkflowStep], order: list[str]
    ) -> tuple[dict[str, str], list[str]]:
        """Where each step will run, resolved, and which ones inherited it.

        One answer, decided in one place, because two callers need it and they
        must agree: materialization writes the folder onto each task, and graph
        validation checks each step's acceptance criterion against the folder
        the step will actually run in.  The shell safety gate exempts absolute
        paths only inside the roots it is handed, so a criterion checked against
        the wrong folder is refused for a reason that has nothing to do with the
        command -- which is why "where does this run" cannot have two answers.
        """
        by_key = {str(step.key).strip(): step for step in steps}
        # A step with no folder of its own runs where the workflow's entry
        # steps run.  A chain is one job, and steps that silently split across
        # directories would write half a result to each.
        inherited = ""
        for key in order:
            step = by_key.get(key)
            if step is not None and step.is_entry() and str(
                step.workspace_root or ""
            ).strip():
                inherited = str(
                    Path(step.workspace_root).expanduser().resolve(strict=False)
                )
                break
        resolved: dict[str, str] = {}
        took_inherited: list[str] = []
        for key in order:
            step = by_key.get(key)
            if step is None:
                continue
            own = str(step.workspace_root or "").strip()
            if not own and inherited:
                took_inherited.append(key)
                resolved[key] = inherited
                continue
            resolved[key] = (
                str(Path(own).expanduser().resolve(strict=False))
                if own
                else str(Path.cwd().resolve())
            )
        return resolved, took_inherited

    def _step_task_spec(
        self,
        step: WorkflowStep,
        trigger: TriggerSpec,
        *,
        workflow_id: str,
        enabled: bool,
        workspace: str,
        request_quote: str = "",
    ) -> NewScheduledTask:
        target = step.delivery_target
        if target is None:
            target = DeliveryTarget.standalone()
        return NewScheduledTask(
            name=step.name,
            kind=step.kind,
            trigger=trigger,
            payload=dict(step.payload),
            delivery_mode=step.delivery_mode,
            delivery_target=target,
            model_override=step.model_override,
            timeout_seconds=int(step.timeout_seconds),
            selected_skills=list(step.selected_skills),
            # Already resolved by ``_step_workspaces``, which is the only place
            # that decides where a step runs.  Resolving again here would be a
            # second answer to the same question.
            workspace_root=workspace,
            permission_profile=step.permission_profile,
            context_policy=step.context_policy,
            # Carried through so the step's own task is judged by the criterion
            # written on the graph.  A step's acceptance is what decides whether
            # the steps below it run, so losing it here would turn a chained
            # workflow back into a sequence of unrelated tasks.
            acceptance=step.acceptance,
            enabled=enabled,
            workflow_id=workflow_id,
            step_key=step.key,
            # Nobody asked for this step by name -- the chain was asked for,
            # and the step is how the chain runs.  So the step carries the
            # chain's words rather than none: a step row that says "asked for
            # by nobody" is indistinguishable from a task that appeared without
            # being asked for, which is the one thing this must not look like.
            request_quote=request_quote,
        )

    @_synchronized
    def materialize_workflow(
        self, workflow_id: str, *, now: Optional[datetime] = None
    ) -> dict[str, Any]:
        """Build or refresh the tasks behind a workflow's steps.

        Steps are walked in dependency order because a downstream step's
        trigger names its upstreams' task ids, so those tasks have to exist
        first.  The order comes from the same function that rejected a cycle,
        so a graph that got this far has one.

        Tasks that already belong to a step are updated rather than replaced.
        Their ids are load-bearing: they are what the downstream subscriptions
        point at, and what the run history hangs from.

        Returns a report of what changed, including which steps inherited a
        project folder from somewhere else -- an inheritance nobody can see is
        indistinguishable from a step about to run in the wrong place.
        """
        workflow = self.get_workflow(workflow_id)
        if workflow is None:
            raise ValueError(f"找不到 workflow：{workflow_id}")
        order = validate_workflow_graph(workflow.steps)
        now_dt = (now or datetime.now(UTC)).astimezone(UTC)
        by_key = {str(step.key).strip(): step for step in workflow.steps}
        existing = self.step_tasks(workflow_id)
        workspaces, took_inherited = self._step_workspaces(workflow.steps, order)

        created: list[str] = []
        updated: list[str] = []
        inherited: list[str] = []
        task_id_by_key: dict[str, str] = {}
        for key in order:
            step = by_key[key]
            upstream_ids = [task_id_by_key[dep] for dep in step.depends_on]
            trigger = step_trigger_spec(step, upstream_ids)
            spec = self._step_task_spec(
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
                task = self.create_task(spec, now=now_dt)
                created.append(task.id)
            else:
                refreshed = self.update_task(current.id, spec, now=now_dt)
                task = refreshed or current
                updated.append(task.id)
            task_id_by_key[key] = task.id

        # A step that is no longer in the graph must stop running.  Its task
        # and its history stay; what goes is its ability to fire.
        removed: list[str] = []
        for key, task in existing.items():
            if key in task_id_by_key:
                continue
            if task.enabled:
                with self._conn:
                    self._conn.execute(
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

    @_synchronized
    def workflow_blocked_steps(
        self, workflow_id: str, failed_step_key: str
    ) -> list[tuple[str, ScheduledTask]]:
        """Steps that can no longer run because *failed_step_key* did not.

        Returns them paired with their tasks, in the graph's own order, because
        the caller has to record a skip against each one and then emit that
        step's own signal so the steps below *it* are reached in turn.
        """
        workflow = self.get_workflow(workflow_id)
        if workflow is None:
            return []
        blocked = workflow_downstream_steps(workflow.steps, failed_step_key)
        if not blocked:
            return []
        tasks = self.step_tasks(workflow_id)
        paired: list[tuple[str, ScheduledTask]] = []
        for step in workflow.steps:
            key = str(step.key).strip()
            if key in blocked and key in tasks:
                paired.append((key, tasks[key]))
        return paired

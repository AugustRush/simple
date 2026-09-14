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
    ClaimedTask,
    DeliveryTarget,
    NewScheduledTask,
    ScheduledTask,
    TaskRun,
    TriggerSpec,
    execution_snapshot,
)


UTC = timezone.utc


def _new_id() -> str:
    return shared._new_id()


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


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
    SCHEMA_VERSION = 7

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

    @_synchronized
    def create_task(
        self, task: NewScheduledTask, now: Optional[datetime] = None
    ) -> ScheduledTask:
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
                    next_run_at, lease_until,
                    active_run_id, last_run_at, last_success_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
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
                    _iso(next_run_at),
                    _iso(created_at),
                    _iso(created_at),
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
                    task_id,
                ),
            )
        return self.get_task(task_id) if cursor.rowcount else None

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
                    delivery_status = ?, finished_at = ?, updated_at = ?
                WHERE id = ? AND task_id = ? AND status = 'running'
                """,
                (
                    status,
                    summary,
                    error,
                    output_path,
                    delivery_status,
                    _iso(finished_at),
                    _iso(finished_at),
                    run_id,
                    task_id,
                ),
            )
            if run_cursor.rowcount != 1:
                raise RuntimeError("owned scheduler run disappeared during completion")
        return True

    @_synchronized
    def set_enabled(self, task_id: str, enabled: bool) -> None:
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
        with self._conn:
            self._conn.execute(
                "DELETE FROM scheduled_task_runs WHERE task_id = ?", (task_id,)
            )
            self._conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))

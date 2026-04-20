from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Optional

from agent import shared
from .models import (
    ClaimedTask,
    DeliveryTarget,
    NewScheduledTask,
    ScheduledTask,
    TaskRun,
    TriggerSpec,
)


UTC = timezone.utc


def _new_id() -> str:
    return shared._new_id()


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class SchedulerStore:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or shared.SCHEDULER_DB_FILE
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def close(self) -> None:
        self._conn.close()

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
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES scheduled_tasks(id)
                );
                CREATE INDEX IF NOT EXISTS idx_scheduled_task_runs_task_id
                    ON scheduled_task_runs(task_id, created_at);
                """
            )

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
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

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
                    overlap_policy, missed_run_policy, next_run_at, lease_until,
                    active_run_id, last_run_at, last_success_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
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
                    _iso(next_run_at),
                    _iso(created_at),
                    _iso(created_at),
                ),
            )
        created = self.get_task(task_id)
        assert created is not None
        return created

    def list_tasks(self) -> list[ScheduledTask]:
        rows = self._conn.execute(
            "SELECT * FROM scheduled_tasks ORDER BY created_at ASC"
        ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def get_task(self, task_id: str) -> Optional[ScheduledTask]:
        row = self._conn.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ? LIMIT 1",
            (task_id,),
        ).fetchone()
        return self._task_from_row(row) if row else None

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

    def claim_due_tasks(
        self, now: datetime, limit: int = 10, lease_seconds: int = 300
    ) -> list[ClaimedTask]:
        now = now.astimezone(UTC)
        rows = self._conn.execute(
            """
            SELECT * FROM scheduled_tasks
            WHERE enabled = 1
              AND next_run_at IS NOT NULL
              AND next_run_at <= ?
              AND (lease_until IS NULL OR lease_until < ?)
            ORDER BY next_run_at ASC, created_at ASC
            LIMIT ?
            """,
            (_iso(now), _iso(now), int(limit)),
        ).fetchall()
        claimed: list[ClaimedTask] = []
        with self._conn:
            for row in rows:
                task = self._task_from_row(row)
                if task.next_run_at is None:
                    continue
                run_id = _new_id()
                started_at = now
                next_run_at = task.trigger.advance_after_claim(task.next_run_at, now)
                lease_until = now + timedelta(seconds=lease_seconds)
                self._conn.execute(
                    """
                    INSERT INTO scheduled_task_runs (
                        id, task_id, scheduled_for, started_at, finished_at, status,
                        summary, error, output_path, delivery_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, 'running', '', '', '', '', ?, ?)
                    """,
                    (
                        run_id,
                        task.id,
                        _iso(task.next_run_at),
                        _iso(started_at),
                        _iso(started_at),
                        _iso(started_at),
                    ),
                )
                self._conn.execute(
                    """
                    UPDATE scheduled_tasks
                    SET next_run_at = ?, lease_until = ?, active_run_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        _iso(next_run_at),
                        _iso(lease_until),
                        run_id,
                        _iso(now),
                        task.id,
                    ),
                )
                refreshed = self.get_task(task.id)
                assert refreshed is not None
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
                            created_at=started_at,
                            updated_at=started_at,
                        ),
                    )
                )
        return claimed

    def recover_stale_runs(self, now: datetime) -> int:
        now = now.astimezone(UTC)
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
        if not rows:
            return 0
        with self._conn:
            for row in rows:
                self._conn.execute(
                    """
                    UPDATE scheduled_task_runs
                    SET status = 'interrupted', finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (_iso(now), _iso(now), row["active_run_id"]),
                )
                self._conn.execute(
                    """
                    UPDATE scheduled_tasks
                    SET next_run_at = ?, lease_until = NULL, active_run_id = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (row["scheduled_for"], _iso(now), row["task_id"]),
                )
        return len(rows)

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
    ) -> None:
        finished_at = finished_at.astimezone(UTC)
        with self._conn:
            self._conn.execute(
                """
                UPDATE scheduled_task_runs
                SET status = ?, summary = ?, error = ?, output_path = ?,
                    delivery_status = ?, finished_at = ?, updated_at = ?
                WHERE id = ?
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
                ),
            )
            if status == "succeeded":
                self._conn.execute(
                    """
                    UPDATE scheduled_tasks
                    SET active_run_id = NULL, lease_until = NULL,
                        last_run_at = ?, last_success_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        _iso(finished_at),
                        _iso(finished_at),
                        _iso(finished_at),
                        task_id,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE scheduled_tasks
                    SET active_run_id = NULL, lease_until = NULL,
                        last_run_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (_iso(finished_at), _iso(finished_at), task_id),
                )

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

    def delete_task(self, task_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM scheduled_task_runs WHERE task_id = ?", (task_id,)
            )
            self._conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))

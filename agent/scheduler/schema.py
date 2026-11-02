"""Schema creation and migration for the scheduler database. Moved out of SchedulerStore (see the remaining-big-files survey work order). The migration table travels with its only reader; the schema version arrives as a collaborator because it is a class attribute."""

from __future__ import annotations

__all__ = [
    "_ensure_schema",
    "_migrate_schema",
]


from agent.scheduler.migrations import (
    _migrate_to_1, _migrate_to_2, _migrate_to_3, _migrate_to_4, _migrate_to_5, _migrate_to_6,
    _migrate_to_7, _migrate_to_8, _migrate_to_9, _migrate_to_10, _migrate_to_11, _migrate_to_12,
    _migrate_to_13, _migrate_to_14, _migrate_to_15,
)

_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migrate_to_1,
    2: _migrate_to_2,
    3: _migrate_to_3,
    4: _migrate_to_4,
    5: _migrate_to_5,
    6: _migrate_to_6,
    7: _migrate_to_7,
    8: _migrate_to_8,
    9: _migrate_to_9,
    10: _migrate_to_10,
    11: _migrate_to_11,
    12: _migrate_to_12,
    13: _migrate_to_13,
    14: _migrate_to_14,
    15: _migrate_to_15,
}


def _ensure_schema(
    conn,
    *,
    schema_version,
) -> None:
    with conn:
        conn.executescript(
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
                    workflow_run_id TEXT NOT NULL DEFAULT '',
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
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
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
        current_version = int(
            conn.execute("PRAGMA user_version").fetchone()[0]
        )
        if current_version < schema_version:
            _migrate_schema(conn, current_version, schema_version)


def _migrate_schema(
    conn,
    current_version: int,
    target_version: int,
) -> None:
    """Apply every migration between two versions, oldest step first.

        The steps live in ``_MIGRATIONS`` and only do the work; the version
        bump belongs here, so a step cannot forget it or record a wrong number.
        """
    for version in range(int(current_version) + 1, target_version + 1):
        step = _MIGRATIONS.get(version)
        if step is None:
            raise RuntimeError(
                f"no scheduler migration for schema version {version}"
            )
        step(conn)
        conn.execute(f"PRAGMA user_version = {version}")

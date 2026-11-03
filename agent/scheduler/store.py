from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import wraps
import json
from pathlib import Path
import sqlite3
from agent.scheduler.rows import (
    _task_from_row,
    _run_from_row,
    _check_acceptance,
    _check_products,
    _identity_values,
    _attention_clause,
    _step_workspaces,
    _step_task_spec,
    _validate_graph,
    _emission_from_row,
    _workflow_from_row,
    _emission_workflow_run_id,
)
from agent.scheduler.workflow_engine import _run_report_payload
from agent.scheduler.migrations import (
    _migrate_to_1,
    _migrate_to_2,
    _migrate_to_3,
    _migrate_to_4,
    _migrate_to_5,
    _migrate_to_6,
    _migrate_to_7,
    _migrate_to_8,
    _migrate_to_9,
    _migrate_to_10,
    _migrate_to_11,
    _migrate_to_12,
    _migrate_to_13,
    _migrate_to_14,
)
import threading
from typing import Any, Callable, Optional, TypeVar

from agent import shared
from . import workflow_editing
from . import retries
from . import attention
from . import workflow_engine
from . import schema
from . import txn_helpers
from . import reads
from .models import (
    optional_datetime,
    _iso,
    _new_id,
    ATTENTION_STATUSES,
    DEFAULT_SIGNAL_MAX_DEPTH,
    RETRYABLE_RUN_STATUSES,
    RUN_SKIPPED_STATUS,
    RUN_SUCCESS_STATUS,
    RUN_UNVERIFIED_STATUS,
    SIGNAL_MODE_ALL,
    TERMINAL_RUN_STATUSES,
    UTC,
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
    declared_products,
    encode_verification,
    execution_snapshot,
    normalize_products,
    parse_task_signal,
    products_for_handoff,
    products_payload,
    signal_mode,
    signal_names,
    step_trigger_spec,
    subscribes_to,
    task_signal_name,
    validate_acceptance,
    validate_products,
    validate_workflow_graph,
    workflow_downstream_steps,
    workflow_step_order,
)








#: The columns that decide whether two rows describe the same task.
#:
#: One list, because this question is asked in two places and two lists that
#: have to agree eventually do not.  ``find_matching_task`` asks it when a
#: create might be a repeat of something already there; the scheduler asks it
#: on every tick, through ``disable_duplicate_enabled_tasks``.  When they
#: disagreed -- by ``acceptance_json`` and ``produces_json`` -- the tick
#: disabled a task that creation had just treated as distinct: two tasks
#: disagreeing about what "done" means, or about which files they leave
#: behind, were folded into one and the later one switched off, which is the
#: opposite of what the create path had promised the caller.
#:
#: ``enabled`` is deliberately absent: both readers filter on it first (the
#: create path because it compares against the switch it is creating with, the
#: sweep because it only ever looks at switched-on rows), so including it here
#: would compare the same thing twice in one place and not at all in the other.
#:
_TASK_IDENTITY_COLUMNS: tuple[str, ...] = (
    "name",
    "kind",
    "trigger_json",
    "payload_json",
    "delivery_mode",
    "delivery_target_json",
    "model_override",
    "workspace_root",
    "context_policy",
    "timeout_seconds",
    "retry_policy_json",
    "selected_skills_json",
    "permission_profile",
    "acceptance_json",
    "produces_json",
    "workflow_id",
    "step_key",
)


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



















    # signal_joins and workflows are created by the DDL above;
    # nothing to backfill for either.  A join's satisfied set starts
    # empty by definition, and a workflow nobody has authored is
    # absent rather than an empty row.












# One entry per schema version.  The driver walks them in order and owns
# the version bump, so a step only has to do the work.


def scheduler_store() -> "SchedulerStore":
    """This process's scheduler store.

    Relocated from ``cli.py``: the scheduler's own builder needs it, and a builder in
    ``agent/scheduler`` importing the CLI would be a cycle.  The CLI keeps a private
    alias so its eleven command bodies read unchanged.
    """
    return open_store()


class SchedulerStore:
    SCHEMA_VERSION = 15

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
    def _calling_in_thread(self) -> "SchedulerStore":
        """A store for this thread to call, owning its own connection.

        A SQLite connection cannot be used from another thread, so the scheduler
        asks the store how it wants to be called rather than testing its class.
        Identity -- ``type(self) is SchedulerStore`` -- so a subclass keeps
        inheriting the parent's connection, exactly as the old class test did:
        this hook moves *where* the decision lives, not *what* it decides.

        Callers own the returned store and should ``close()`` it; the task does,
        in a ``finally``.
        """
        if type(self) is SchedulerStore:
            return SchedulerStore(db_path=self.db_path)
        return self

    def close(self) -> None:
        self._conn.close()

    def _immediate_transaction(self):
        return workflow_engine._immediate_transaction(
            self._conn,
        )


    def _ensure_schema(self) -> None:
        schema._ensure_schema(
            self._conn, schema_version=self.SCHEMA_VERSION
        )


    def _migrate_schema(self, current_version: int, target_version: int) -> None:
        """Apply every migration between two versions, oldest step first."""
        schema._migrate_schema(
            self._conn,
            current_version=current_version,
            target_version=target_version,
        )






    @_synchronized
    def create_task(
        self, task: NewScheduledTask, now: Optional[datetime] = None
    ) -> ScheduledTask:
        with self._immediate_transaction():
            task_id = self._create_task_in_transaction(task, now=now)
        created = self.get_task(task_id)
        assert created is not None
        return created

    def _create_task_in_transaction(
        self, task: NewScheduledTask, *, now: Optional[datetime] = None
    ) -> str:
        """Insert one task and return its id.  Caller owns the transaction."""
        return txn_helpers._create_task_in_transaction(
            self._conn,
            task=task,
            now=now,
        )



    @_synchronized
    def find_matching_task(self, task: NewScheduledTask) -> Optional[ScheduledTask]:
        clauses = []
        params: list[Any] = []
        for column, value in zip(_TASK_IDENTITY_COLUMNS, _identity_values(task)):
            if column == "model_override":
                # `= NULL` matches nothing, and "no model override" is a value
                # this identity has to compare like any other.
                clauses.append(f"(({column} IS NULL AND ? IS NULL) OR {column} = ?)")
                params.extend([value, value])
            else:
                clauses.append(f"{column} = ?")
                params.append(value)
        clauses.append("enabled = ?")
        params.append(1 if task.enabled else 0)
        # The column names are this module's own constants, never caller input.
        row = self._conn.execute(
            "SELECT * FROM scheduled_tasks WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at ASC LIMIT 1",
            tuple(params),
        ).fetchone()
        return _task_from_row(row) if row else None

    @_synchronized
    def disable_duplicate_enabled_tasks(self, now: Optional[datetime] = None) -> int:
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
            # The same columns the create path compares, from the same list --
            # this sweep and the create path have to reach the same verdict
            # about the same pair of rows, or one of them disables something
            # the other just promised to keep.
            signature = tuple(row[column] for column in _TASK_IDENTITY_COLUMNS)
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
        return reads.list_tasks(
            self._conn,
        )


    @_synchronized
    def get_task(self, task_id: str) -> Optional[ScheduledTask]:
        return workflow_engine.get_task(
            self._conn,
            task_id=task_id,
        )



    @_synchronized
    def recent_runs(
        self, limit: int = 20, *, before: Any = None
    ) -> list[tuple[Any, Any]]:
        """Every task's runs, newest first -- see ``reads.recent_runs``."""
        return reads.recent_runs(self._conn, limit=limit, before=before)

    @_synchronized
    def list_runs(self, task_id: str) -> list[TaskRun]:
        return reads.list_runs(
            self._conn,
            task_id=task_id,
        )


    @_synchronized
    def get_run(self, task_id: str, run_id: str) -> Optional[TaskRun]:
        return reads.get_run(
            self._conn,
            task_id=task_id,
            run_id=run_id,
        )


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
        """Insert one pending emission.  The caller owns the transaction."""
        return workflow_engine._insert_emission(
            self._conn,
            name=name,
            payload=payload,
            source=source,
            depth=depth,
            origin_id=origin_id,
            now=now,
        )



    @_synchronized
    def get_emission(self, emission_id: str) -> Optional[SignalEmission]:
        row = self._conn.execute(
            "SELECT * FROM signal_emissions WHERE id = ?", (emission_id,)
        ).fetchone()
        return _emission_from_row(row) if row else None

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
        return [_emission_from_row(row) for row in rows]

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
            return f"运行不会以「{status}」结束；可用的状态：" + "、".join(
                TERMINAL_RUN_STATUSES
            )
        if self.get_task(task_id) is None:
            return f"找不到 id 为 {task_id} 的任务"
        return ""

    def _signal_subscribers(self, name: str) -> list[ScheduledTask]:
        """Enabled tasks waiting on this exact name."""
        return workflow_engine._signal_subscribers(
            self._conn,
            name=name,
        )




    def _satisfied_joins(self, task_id: str, workflow_run_id: str = "") -> set[str]:
        return workflow_engine._satisfied_joins(
            self._conn,
            task_id=task_id,
            workflow_run_id=workflow_run_id,
        )



    def _advance_join(
        self, task: ScheduledTask, emission: SignalEmission, now: datetime
    ) -> list[str]:
        """Record one arrival at a join.  Returns the names still missing."""
        return workflow_engine._advance_join(
            self._conn,
            task=task,
            emission=emission,
            now=now,
        )


    def _join_arrivals(
        self, task_id: str, workflow_run_id: str = ""
    ) -> dict[str, dict[str, Any]]:
        """The payloads a join has collected since its last completed round."""
        return workflow_engine._join_arrivals(
            self._conn,
            task_id=task_id,
            workflow_run_id=workflow_run_id,
        )



    def join_progress(self, task_id: str) -> dict[str, Any]:
        """What a join has heard from so far, so the interface can show it.

        Without this, a join that is one name short looks exactly like a task
        that is broken -- nothing has run and nothing says why.

        For a step of a workflow the answer is the open round's, not the
        round-less bucket's: an arrival with no round attached cannot complete
        a round, so counting it as "heard from" would hide the report the
        round is actually short of.  The round-less bucket is what the answer
        falls back to only when no round has heard anything -- which is the
        case where it is the only thing there is.
        """
        row = self._conn.execute(
            "SELECT trigger_json, workflow_id FROM scheduled_tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        required: list[str] = []
        workflow_id = ""
        if row is not None:
            trigger = TriggerSpec.from_json(row["trigger_json"])
            if signal_mode(trigger) == SIGNAL_MODE_ALL:
                required = signal_names(trigger)
            workflow_id = str(row["workflow_id"] or "")
        satisfied: set[str] = set()
        if workflow_id:
            latest_workflow_round = self._conn.execute(
                """
                SELECT j.workflow_run_id FROM workflow_signal_joins j
                JOIN workflow_runs w
                  ON w.id = j.workflow_run_id AND w.status = 'running'
                WHERE j.task_id = ?
                ORDER BY j.updated_at DESC, j.rowid DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if latest_workflow_round is not None:
                satisfied = self._satisfied_joins(
                    task_id, str(latest_workflow_round["workflow_run_id"])
                )
        if not satisfied:
            satisfied = self._satisfied_joins(task_id)
        return {
            "required": required,
            "satisfied": sorted(satisfied),
            "missing": [name for name in required if name not in satisfied],
        }

    def _take_join(
        self, task_id: str, workflow_run_id: str = ""
    ) -> dict[str, dict[str, Any]]:
        """Read a join's collected arrivals, then spend them.  Caller owns the txn."""
        return workflow_engine._take_join(
            self._conn,
            task_id=task_id,
            workflow_run_id=workflow_run_id,
        )


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
        workflow_engine._record_delivery(
            self._conn,
            emission_id=emission_id,
            task_id=task_id,
            outcome=outcome,
            run_id=run_id,
            reason=reason,
            now=now,
        )



    def _pending_run_for(
        self, task_id: str, workflow_run_id: str = ""
    ) -> Optional[str]:
        """The run already waiting for, or running, this task, if any."""
        return workflow_engine._pending_run_for(
            self._conn,
            task_id=task_id,
            workflow_run_id=workflow_run_id,
        )



    def _round_waiting_on(self, task: ScheduledTask) -> str:
        """The open round whose join is still short this task's success report."""
        return workflow_engine._round_waiting_on(
            self._conn,
            task=task,
        )


    def _prepare_workflow_run(
        self,
        task: ScheduledTask,
        run_id: str,
        snapshot: dict[str, Any],
        *,
        now: datetime,
        trigger_source: str,
    ) -> tuple[dict[str, Any], str]:
        """Attach a run to one durable workflow execution."""
        return workflow_engine._prepare_workflow_run(
            self._conn,
            task=task,
            run_id=run_id,
            snapshot=snapshot,
            now=now,
            trigger_source=trigger_source,
        )


    def _enqueue_signal_run_in_transaction(
        self,
        task: ScheduledTask,
        emission: SignalEmission,
        now: datetime,
        arrivals: Optional[dict[str, dict[str, Any]]] = None,
    ) -> str:
        """Queue one run for a subscriber, carrying the emission it answers."""
        return workflow_engine._enqueue_signal_run_in_transaction(
            self._conn,
            task=task,
            emission=emission,
            now=now,
            arrivals=arrivals,
        )


    @_synchronized
    def deliver_signals(
        self,
        now: Optional[datetime] = None,
        *,
        limit: int = 50,
        max_depth: int = DEFAULT_SIGNAL_MAX_DEPTH,
    ) -> dict[str, int]:
        """Turn waiting emissions into runs.  Returns a tally *by emission*."""
        return workflow_engine.deliver_signals(
            self._conn,
            now=now,
            limit=limit,
            max_depth=max_depth,
        )


    def _settle_emission(
        self,
        emission: SignalEmission,
        *,
        state: str,
        reason: str,
        now: datetime,
        run_id: str = "",
    ) -> None:
        workflow_engine._settle_emission(
            self._conn,
            emission=emission,
            state=state,
            reason=reason,
            now=now,
            run_id=run_id,
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
        """Announce that a run reached ``status``.  Caller owns the transaction."""
        workflow_engine._emit_run_signal(
            self._conn,
            task_id=task_id,
            run_id=run_id,
            status=status,
            finished_at=finished_at,
            summary=summary,
            output_path=output_path,
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


    @_synchronized
    def unacknowledged_attention_counts(self) -> dict[str, int]:
        """Task id -> number of runs nobody has looked at yet."""
        clause, params = _attention_clause()
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
    def unacknowledged_attention_runs(self, limit: int = 200) -> list[TaskRun]:
        """The runs nobody has looked at yet, newest first.

        The list behind :meth:`unacknowledged_attention_counts`, and the two
        are kept together on purpose: a badge that says a number and a page
        that then lists a different set of runs is a number the person cannot
        check, which is worse than no number.  Both read through
        :meth:`_attention_clause`, so adding a status changes them together.

        Capped because this is what a poll asks for.  The count stays the true
        total rather than the size of what came back, so a capped list reads as
        a truncated list instead of quietly lowering the number on the badge.
        """
        clause, params = _attention_clause()
        rows = self._conn.execute(
            f"""
            SELECT * FROM scheduled_task_runs
            WHERE {clause} AND acknowledged_at IS NULL
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            [*params, max(1, int(limit))],
        ).fetchall()
        return [_run_from_row(row) for row in rows]

    @_synchronized
    def attention_snapshot(self, limit: int = 200) -> dict[str, Any]:
        """The badge number, the rows behind it, and each task's newest"""
        return attention.attention_snapshot(
            self._conn,
            limit=limit,
        )


    @_synchronized
    def acknowledge_run(
        self, task_id: str, run_id: str, now: Optional[datetime] = None
    ) -> bool:
        """Mark one run as seen.  False when there was nothing to see."""
        clause, params = _attention_clause()
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
        clause, params = _attention_clause()
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
        return _run_from_row(row) if row else None

    @_synchronized
    def update_task(
        self,
        task_id: str,
        task: NewScheduledTask,
        *,
        now: Optional[datetime] = None,
    ) -> Optional[ScheduledTask]:
        with self._immediate_transaction():
            changed = self._update_task_in_transaction(task_id, task, now=now)
        return self.get_task(task_id) if changed else None

    def _update_task_in_transaction(
        self,
        task_id: str,
        task: NewScheduledTask,
        *,
        now: Optional[datetime] = None,
    ) -> bool:
        """Rewrite one task.  False when there was no such row."""
        return txn_helpers._update_task_in_transaction(
            self._conn,
            task_id=task_id,
            task=task,
            now=now,
        )


    def _upstream_reports_in_transaction(
        self, task: ScheduledTask
    ) -> dict[str, dict[str, Any]]:
        """What the runs this task waits for last produced, read off its trigger."""
        return attention._upstream_reports_in_transaction(
            self._conn,
            task=task,
        )


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
        return retries._claim_task_in_transaction(
            self._conn,
            task=task,
            now=now,
            lease_seconds=lease_seconds,
            scheduled_for=scheduled_for,
            trigger_source=trigger_source,
            snapshot=snapshot,
            attempt=attempt,
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
            task = _task_from_row(row)
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
        return retries.claim_retry(
            self._conn,
            task_id=task_id,
            run_id=run_id,
            use_latest=use_latest,
            now=now,
            lease_seconds=lease_seconds,
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
    def cancel_requested(self, task_id: str, run_id: str) -> bool:
        row = self._conn.execute(
            """
            SELECT 1 FROM scheduled_task_runs
            WHERE id = ? AND task_id = ? AND status = 'running'
              AND cancel_requested_at IS NOT NULL
            LIMIT 1
            """,
            (run_id, task_id),
        ).fetchone()
        return row is not None

    @_synchronized
    def enqueue_retry(
        self,
        task_id: str,
        source_run_id: str,
        *,
        retry_at: datetime,
    ) -> Optional[TaskRun]:
        return retries.enqueue_retry(
            self._conn,
            task_id=task_id,
            source_run_id=source_run_id,
            retry_at=retry_at,
        )


    @_synchronized
    def claim_due_tasks(
        self, now: datetime, limit: int = 10, lease_seconds: int = 300
    ) -> list[ClaimedTask]:
        return workflow_engine.claim_due_tasks(
            self._conn,
            now=now,
            limit=limit,
            lease_seconds=lease_seconds,
        )


    @_synchronized
    def recover_stale_runs(self, now: datetime) -> int:
        now = now.astimezone(UTC)
        with self._immediate_transaction():
            return self._recover_stale_runs_in_transaction(now)

    def _recover_stale_runs_in_transaction(self, now: datetime) -> int:
        return workflow_engine._recover_stale_runs_in_transaction(
            self._conn,
            now=now,
        )


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
        return retries.release_claim(
            self._conn,
            task_id=task_id,
            run_id=run_id,
            now=now,
            reason=reason,
        )


    @_synchronized
    def owns_unexpired_lease(self, task_id: str, run_id: str, *, now: datetime) -> bool:
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
        workflow_run_id: str = "",
        workflow_steps: Optional[list[WorkflowStep]] = None,
    ) -> list[str]:
        """Record the steps this failure blocks as skipped.  Caller owns the txn."""
        return workflow_engine._skip_blocked_steps_in_transaction(
            self._conn,
            workflow_id=workflow_id,
            step_key=step_key,
            status=status,
            finished_at=finished_at,
            workflow_run_id=workflow_run_id,
            workflow_steps=workflow_steps,
        )


    def _workflow_run_steps(self, workflow_run_id: str) -> list[WorkflowStep]:
        return workflow_engine._workflow_run_steps(
            self._conn,
            workflow_run_id=workflow_run_id,
        )



    def _settle_workflow_run(self, workflow_run_id: str, now: datetime) -> None:
        """Roll up a workflow execution once nothing of it is still in flight."""
        workflow_engine._settle_workflow_run(
            self._conn,
            workflow_run_id=workflow_run_id,
            now=now,
        )


    def _settle_workflow_run_of_run(self, run_id: str, now: datetime) -> None:
        """Close out the workflow round one run belonged to.  Caller owns txn."""
        workflow_engine._settle_workflow_run_of_run(
            self._conn,
            run_id=run_id,
            now=now,
        )


    def _close_rounds_before_edit(self, workflow_id: str, now: datetime) -> None:
        """End every round that is not executing something.  Caller owns txn."""
        workflow_editing._close_rounds_before_edit(
            self._conn,
            workflow_id=workflow_id,
            now=now,
        )


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
        products: Any = None,
        retry_at: Optional[datetime] = None,
    ) -> bool:
        return workflow_engine.complete_run(
            self._conn,
            task_id=task_id,
            run_id=run_id,
            finished_at=finished_at,
            status=status,
            summary=summary,
            error=error,
            output_path=output_path,
            delivery_status=delivery_status,
            verdict=verdict,
            verification=verification,
            products=products,
            retry_at=retry_at,
        )


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
            self._conn.execute("DELETE FROM signal_joins WHERE task_id = ?", (task_id,))
            self._conn.execute(
                "DELETE FROM workflow_signal_joins WHERE task_id = ?", (task_id,)
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



    @_synchronized
    def create_workflow(
        self, workflow: Workflow, *, now: Optional[datetime] = None
    ) -> Workflow:
        """Store a graph, then build the tasks it describes.

        The two happen together on purpose, and in one transaction.  A graph
        with no tasks behind it is a drawing: it looks like a plan and runs
        nothing, and the person who made it has no way to tell the difference
        from the outside.  Committing the graph first would publish exactly that
        drawing for as long as the build took, and leave it standing for good if
        the build failed.
        """
        _validate_graph(workflow.steps)
        created_at = (now or datetime.now(UTC)).astimezone(UTC)
        workflow_id = _new_id()
        with self._immediate_transaction():
            self._conn.execute(
                """
                INSERT INTO workflows (
                    id, name, description, enabled, graph_json,
                    request_quote, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
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
            self._materialize_workflow_in_transaction(workflow_id, now=created_at)
        stored = self.get_workflow(workflow_id)
        assert stored is not None
        return stored

    @_synchronized
    def get_workflow(self, workflow_id: str) -> Optional[Workflow]:
        return workflow_engine.get_workflow(
            self._conn,
            workflow_id=workflow_id,
        )


    @_synchronized
    def list_workflows(self) -> list[Workflow]:
        rows = self._conn.execute(
            "SELECT * FROM workflows ORDER BY created_at ASC, id ASC"
        ).fetchall()
        return [_workflow_from_row(row) for row in rows]

    @_synchronized
    def update_workflow(
        self,
        workflow_id: str,
        workflow: Workflow,
        *,
        now: Optional[datetime] = None,
        materialize: bool = True,
    ) -> Optional[Workflow]:
        """Store a new graph, and by default rebuild the tasks behind it."""
        return workflow_editing.update_workflow(
            self._conn,
            workflow_id=workflow_id,
            workflow=workflow,
            now=now,
            materialize=materialize,
        )


    @_synchronized
    def delete_workflow(
        self, workflow_id: str, *, now: Optional[datetime] = None
    ) -> list[str]:
        """Drop the graph and disable the tasks it built.  Returns their ids."""
        return workflow_editing.delete_workflow(
            self._conn,
            workflow_id=workflow_id,
            now=now,
        )


    @_synchronized
    def step_tasks(self, workflow_id: str) -> dict[str, ScheduledTask]:
        """The tasks behind a workflow's steps, keyed by step key."""
        return workflow_editing.step_tasks(
            self._conn,
            workflow_id=workflow_id,
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
        with self._immediate_transaction():
            return self._materialize_workflow_in_transaction(workflow_id, now=now)

    def _materialize_workflow_in_transaction(
        self, workflow_id: str, *, now: Optional[datetime] = None
    ) -> dict[str, Any]:
        """Body of :meth:`materialize_workflow`.  Caller owns the transaction."""
        return workflow_editing._materialize_workflow_in_transaction(
            self._conn,
            workflow_id=workflow_id,
            now=now,
        )


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


def open_store(db_path: Optional[Path] = None) -> "SchedulerStore":
    """The one place a ``SchedulerStore`` is constructed.

    Why this exists
    ---------------
    ``SchedulerStore(db_path=shared.SCHEDULER_DB_FILE)`` was written out in
    twenty-two places -- twenty in the web channel alone, one in the CLI and one
    in a builtin tool -- and every one of them resolved the same module-level
    global, which ``_set_agent_home`` rewrites at runtime.  Twenty-two readers of
    one mutable global is the same shape as the ``components`` backdoor that
    Task 4 removed, and it has the same consequence: the database a caller talks
    to is a property of the process, not of the caller, and there is no seam at
    which a test or another composition can point somewhere else.

    The parameter is therefore the point.  ``None`` keeps the configured default,
    which is what every existing call site wanted, and an explicit path lets a
    caller -- a test, a second instance, a future plugin -- choose its own.

    Callers own the returned store and must ``close()`` it; this does not manage
    a connection pool, because two of the call sites deliberately hold one open
    for the life of a service.
    """
    return SchedulerStore(db_path=db_path)

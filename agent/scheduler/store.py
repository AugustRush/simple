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
    SCHEMA_VERSION = 14

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
        """The badge number, the rows behind it, and each task's newest
        waiting run — as one answer, read under one lock.

        The endpoints that serve attention send all of these together, and
        the caller is invited to add up the rows and check them against the
        number.  That invitation is only honest when all of it comes from one
        snapshot: reading the count and the list through separate
        :meth:`_synchronized` calls leaves a gap between them that the
        scheduler thread writes through, and a number the page cannot
        reproduce is the thing this exists to prevent.

        Only ``runs`` is capped — that is what a poll asks for.  ``counts``,
        ``total`` and ``latest_by_task`` are read without the cap, because
        each answers a question about tasks the capped list never mentions.
        A task whose rows all sort below the cap still shows a count on its
        card, and clicking that count still has to land on a run; deriving
        these three from the capped rows drops such a task outright, which
        makes ``sum(counts)`` disagree with ``total`` and leaves the card
        with nothing to open.  The cap bounds the payload, not the truth.

        ``total`` is the sum of ``counts`` rather than its own aggregate, so
        the badge number is arithmetically the per-task numbers beside it —
        the one relationship the page is invited to check.
        """
        clause, params = _attention_clause()
        # One row per task, over every waiting run: the task's count and the
        # id of its newest one.  Two narrow columns rather than the whole row,
        # since this query exists to be exhaustive and the rows themselves are
        # what the capped query below is for.
        per_task = self._conn.execute(
            f"""
            SELECT task_id, id, task_total FROM (
                SELECT task_id, id,
                       COUNT(*) OVER (PARTITION BY task_id) AS task_total,
                       ROW_NUMBER() OVER (
                           PARTITION BY task_id
                           ORDER BY created_at DESC, id DESC
                       ) AS task_rank
                FROM scheduled_task_runs
                WHERE {clause} AND acknowledged_at IS NULL
            )
            WHERE task_rank = 1
            """,
            params,
        ).fetchall()
        counts: dict[str, int] = {}
        latest_by_task: dict[str, str] = {}
        for row in per_task:
            task_id = str(row["task_id"])
            counts[task_id] = int(row["task_total"])
            latest_by_task[task_id] = str(row["id"])
        rows = self._conn.execute(
            f"""
            SELECT * FROM scheduled_task_runs
            WHERE {clause} AND acknowledged_at IS NULL
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            [*params, max(1, int(limit))],
        ).fetchall()
        return {
            "total": sum(counts.values()),
            "counts": counts,
            "runs": [_run_from_row(row) for row in rows],
            "latest_by_task": latest_by_task,
        }

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
                # The upstream's declared products and what its run actually
                # left, both read here so that a step started by hand is told
                # exactly what one woken by a signal is told.  Reading only the
                # emission's copy would make "what can I count on" depend on
                # how the run started, which is what this method exists to
                # stop.
                declares=json.loads(upstream["produces_json"] or "[]"),
                products=json.loads(latest["products_json"] or "[]"),
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
        snapshot, workflow_run_id = self._prepare_workflow_run(
            task,
            run_id,
            snapshot,
            now=now,
            trigger_source=trigger_source,
        )
        self._conn.execute(
            """
            INSERT INTO scheduled_task_runs (
                id, task_id, scheduled_for, started_at, finished_at, status,
                summary, error, output_path, delivery_status,
                config_snapshot_json, trigger_source, attempt,
                workflow_run_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, 'running', '', '', '', '', ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                task.id,
                _iso(scheduled_for),
                _iso(now),
                json.dumps(snapshot, ensure_ascii=False),
                trigger_source,
                max(1, int(attempt)),
                workflow_run_id,
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
        claimed_task = _task_from_row(refreshed) if refreshed else task
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
                workflow_run_id=workflow_run_id,
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
            task = _task_from_row(task_row)
            source = _run_from_row(source_row)
            snapshot = (
                execution_snapshot(task) if use_latest else source.config_snapshot
            )
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
                return _run_from_row(existing)
            source = _run_from_row(source_row)
            run_id = _new_id()
            self._conn.execute(
                """
                INSERT INTO scheduled_task_runs (
                    id, task_id, scheduled_for, started_at, finished_at, status,
                    summary, error, output_path, delivery_status,
                    config_snapshot_json, trigger_source, attempt,
                    cancel_requested_at, retry_of_run_id, workflow_run_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, 'queued', '', '', '', '', ?,
                          'automatic_retry', ?, NULL, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    task_id,
                    _iso(source.scheduled_for),
                    _iso(retry_at),
                    json.dumps(source.config_snapshot, ensure_ascii=False),
                    source.attempt + 1,
                    source_run_id,
                    source.workflow_run_id,
                    _iso(datetime.now(UTC)),
                    _iso(datetime.now(UTC)),
                ),
            )
            row = self._conn.execute(
                "SELECT * FROM scheduled_task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return _run_from_row(row) if row else None

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
            # A released claim is over: nothing completes it afterwards, so if
            # it was the only live work of its workflow round, the round is
            # rolled up here.  Same reasoning as the stale-lease recovery
            # above -- every way a run ends has to close the round it ends.
            self._settle_workflow_run_of_run(run_id, now)
        return True

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
        """End every round that is not executing something.  Caller owns txn.

        An edit may proceed while a round is ``running`` only if that round has
        no queued or running work: work in flight is executing the definition
        being replaced, and refusing there is the point of the check.  A round
        whose remaining steps are simply not coming -- the second clock entry
        of a dual-entry workflow, a join waiting on an upstream nobody will
        run -- has nothing in flight and can never complete on its own, so
        leaving it ``running`` turned one manual run into an edit lock that
        only deleting the workflow could open.  It is closed here, interrupted,
        because that is what it is: an execution the scheduler stopped being
        able to finish.

        Deliberately not a periodic sweep.  A round with nothing in flight is
        still rescuable -- manually running the upstream its join waits on
        joins that round (see :meth:`_round_waiting_on`) -- so closing it on a
        timer would take that away.  It is closed when somebody edits the
        definition, which is the moment the old round is declared over.
        """
        rounds = self._conn.execute(
            "SELECT id FROM workflow_runs WHERE workflow_id = ? AND status = 'running'",
            (workflow_id,),
        ).fetchall()
        for row in rounds:
            round_id = str(row["id"])
            self._settle_workflow_run(round_id, now)
            live = self._conn.execute(
                """
                SELECT 1 FROM scheduled_task_runs
                WHERE workflow_run_id = ? AND status IN ('queued', 'running')
                LIMIT 1
                """,
                (round_id,),
            ).fetchone()
            if live is not None:
                raise ValueError(
                    "workflow 仍有执行中的轮次，需等待完成或取消后再修改定义"
                )
            self._conn.execute(
                """
                UPDATE workflow_runs
                SET status = 'interrupted', updated_at = ?, finished_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (_iso(now), _iso(now), round_id),
            )
            # Half-collected arrivals belong to the round that just ended, and
            # the graph they were waiting on is about to be replaced.  Left
            # behind they would complete a round nobody opened, on a report
            # from before the edit.
            self._conn.execute(
                "DELETE FROM workflow_signal_joins WHERE workflow_run_id = ?",
                (round_id,),
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
        """Store a new graph, and by default rebuild the tasks behind it.

        ``materialize=False`` is for the one caller that has just written a
        step's *task* and is copying that change back into the graph: the task
        is the newer of the two, and rebuilding it from the step that was
        copied from it would be a round trip that only risks losing something.

        The new graph and the tasks rebuilt from it are one transaction, so an
        edit that fails halfway leaves the previous version running rather than
        a graph whose steps disagree with the tasks behind them.
        """
        if self.get_workflow(workflow_id) is None:
            return None
        _validate_graph(workflow.steps)
        updated_at = (now or datetime.now(UTC)).astimezone(UTC)
        with self._immediate_transaction():
            self._close_rounds_before_edit(workflow_id, updated_at)
            # `request_quote` is deliberately absent here.  It records who asked
            # for the chain, and editing a step does not change that -- while a
            # caller that rebuilds the graph from an edit form has no quote to
            # send, so writing the column from `workflow` would blank the one
            # piece of evidence the row holds about its own origin.
            self._conn.execute(
                """
                UPDATE workflows
                SET name = ?, description = ?, enabled = ?, graph_json = ?,
                    version = version + 1, updated_at = ?
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
                self._materialize_workflow_in_transaction(workflow_id, now=updated_at)
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

        A join that was part-way through its round does *not* keep that
        progress.  Unlike the run history, a half-satisfied round is not a
        record of something that happened -- it is an expectation about what
        happens next, and the graph it was waiting on no longer exists.  Left
        behind, it survives until somebody switches one of these orphaned
        steps back on (which they may, since a step whose workflow is gone is
        an ordinary task again), and then the first report from any one arm
        finds the set already full and closes a round whose other arms are
        still disabled and can never report.
        """
        now_dt = (now or datetime.now(UTC)).astimezone(UTC)
        disabled = [task.id for task in self.step_tasks(workflow_id).values()]
        with self._immediate_transaction():
            if disabled:
                placeholders = ", ".join("?" for _ in disabled)
                self._conn.execute(
                    f"""
                    UPDATE scheduled_task_runs
                    SET status = 'cancelled', finished_at = ?, updated_at = ?,
                        error = CASE WHEN error = '' THEN 'workflow deleted' ELSE error END
                    WHERE task_id IN ({placeholders}) AND status = 'queued'
                    """,
                    (_iso(now_dt), _iso(now_dt), *disabled),
                )
                self._conn.execute(
                    f"""
                    UPDATE scheduled_task_runs
                    SET cancel_requested_at = COALESCE(cancel_requested_at, ?),
                        updated_at = ?
                    WHERE task_id IN ({placeholders}) AND status = 'running'
                    """,
                    (_iso(now_dt), _iso(now_dt), *disabled),
                )
            self._conn.execute(
                """
                UPDATE workflow_runs
                SET status = 'cancelled', updated_at = ?, finished_at = ?
                WHERE workflow_id = ? AND status = 'running'
                """,
                (_iso(now_dt), _iso(now_dt), workflow_id),
            )
            for task_id in disabled:
                self._conn.execute(
                    """
                    UPDATE scheduled_tasks
                    SET enabled = 0, next_run_at = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (_iso(now_dt), task_id),
                )
                self._conn.execute(
                    "DELETE FROM signal_joins WHERE task_id = ?", (task_id,)
                )
                self._conn.execute(
                    "DELETE FROM workflow_signal_joins WHERE task_id = ?", (task_id,)
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
        return {row["step_key"]: _task_from_row(row) for row in rows}



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
        """Body of :meth:`materialize_workflow`.  Caller owns the transaction.

        Split out so that storing a graph and building the tasks behind it can
        be one transaction rather than two.  The graph row is what makes a
        workflow visible; committing it before its tasks exist leaves, for as
        long as the build takes and forever if the build fails, exactly the
        drawing that :meth:`create_workflow` exists to avoid.
        """
        workflow = self.get_workflow(workflow_id)
        if workflow is None:
            raise ValueError(f"找不到 workflow：{workflow_id}")
        order = validate_workflow_graph(workflow.steps)
        now_dt = (now or datetime.now(UTC)).astimezone(UTC)
        by_key = {str(step.key).strip(): step for step in workflow.steps}
        existing = self.step_tasks(workflow_id)
        workspaces, took_inherited = _step_workspaces(workflow.steps, order)

        created: list[str] = []
        updated: list[str] = []
        inherited: list[str] = []
        task_id_by_key: dict[str, str] = {}
        removed: list[str] = []
        # One transaction for the whole graph.  The workflow row is written
        # before any of its tasks exist, so a failure partway through this loop
        # would leave a graph whose later steps have nothing to run them, while
        # the steps that did get built are already subscribed to task ids the
        # rest of the graph will never be told about.  A half-materialized
        # workflow looks enabled and fires its head, then stops silently.
        for key in order:
            step = by_key[key]
            upstream_ids = [task_id_by_key[dep] for dep in step.depends_on]
            trigger = step_trigger_spec(step, upstream_ids)
            spec = _step_task_spec(
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
                task_id = self._create_task_in_transaction(spec, now=now_dt)
                created.append(task_id)
            else:
                self._update_task_in_transaction(current.id, spec, now=now_dt)
                task_id = current.id
                updated.append(task_id)
            task_id_by_key[key] = task_id

        # A step that is no longer in the graph must stop running.  Its task
        # and its history stay; what goes is its ability to fire.
        for key, task in existing.items():
            if key in task_id_by_key:
                continue
            if task.enabled:
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

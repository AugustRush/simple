"""The workflow and signal engine: claiming due tasks, settling runs, delivering signals, and the report payload a run carries. Moved out of SchedulerStore (see the remaining-big-files survey work order)."""

from __future__ import annotations
import json
from agent.scheduler.rows import _emission_from_row, _emission_workflow_run_id, _run_from_row, _task_from_row, _workflow_from_row
from contextlib import contextmanager
from datetime import datetime, timedelta
from . import reads, txn_helpers
from .models import products_for_handoff, ClaimedTask, DEFAULT_SIGNAL_MAX_DEPTH, RETRYABLE_RUN_STATUSES, RUN_SKIPPED_STATUS, RUN_SUCCESS_STATUS, RUN_UNVERIFIED_STATUS, SIGNAL_MODE_ALL, ScheduledTask, SignalEmission, TERMINAL_RUN_STATUSES, TaskRun, UTC, Workflow, WorkflowStep, _iso, _new_id, encode_verification, execution_snapshot, products_payload, signal_mode, signal_names, task_signal_name, workflow_downstream_steps
from pathlib import Path
from typing import Any, Optional

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
    workflow_run_id: str = "",
    summary: str = "",
    output_path: str = "",
    declares: Any = None,
    products: Any = None,
) -> dict[str, Any]:
    """One run's report, in the shape every reader of it expects.

    Two things produce this: the emission a run makes when it finishes, and
    the graph, read at the moment a run is claimed.  They have to agree.  A
    step told one thing when a signal woke it and another when somebody
    started it by hand is a step whose behaviour depends on how it started,
    which is exactly the distinction the handoff is supposed to erase.

    ``products`` is the address of what the run promised to leave behind --
    the half of the handoff that used to be missing.  ``output_path`` says
    where the run's *answer* went; these say where the *files* it produced
    are, and they are different things: a report can be generated and stored
    without the CSV anybody downstream wanted ever being written.  Paths that
    were promised but are not there travel as ``products_missing`` rather than
    as addresses, because handing over a path with no file behind it is how a
    downstream step reads an older round's work and believes it is this one's.
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
    if workflow_run_id:
        payload["workflow_run_id"] = workflow_run_id
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
    handoff = products_for_handoff(declares, products)
    if handoff["products"]:
        payload["products"] = handoff["products"]
    if handoff["products_missing"]:
        payload["products_missing"] = handoff["products_missing"]
    return payload


@contextmanager
def _immediate_transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def get_task(
    conn,
    task_id: str,
) -> Optional[ScheduledTask]:
    return reads.get_task(
        conn,
        task_id=task_id,
    )


def _insert_emission(
    conn,
    name: str,
    payload: Optional[dict[str, Any]] = None,
    *,
    source: str = "manual",
    depth: int = 0,
    origin_id: str = "",
    now: datetime,
) -> SignalEmission:
    """Insert one pending emission.  The caller owns the transaction."""
    return txn_helpers._insert_emission(
        conn,
        name=name,
        payload=payload,
        source=source,
        depth=depth,
        origin_id=origin_id,
        now=now,
    )


def _signal_subscribers(
    conn,
    name: str,
) -> list[ScheduledTask]:
    """Enabled tasks waiting on this exact name."""
    return txn_helpers._signal_subscribers(
        conn,
        name=name,
    )


def _satisfied_joins(
    conn,
    task_id: str,
    workflow_run_id: str = "",
) -> set[str]:
    return txn_helpers._satisfied_joins(
        conn,
        task_id=task_id,
        workflow_run_id=workflow_run_id,
    )


def _advance_join(
    conn,
    task: ScheduledTask,
    emission: SignalEmission,
    now: datetime,
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
    workflow_run_id = _emission_workflow_run_id(emission)
    satisfied = _satisfied_joins(conn, task.id, workflow_run_id)
    satisfied.add(emission.name)
    # Intersected with what this join actually waits for, so a name that
    # this task is not subscribed to can never contribute to completing it.
    satisfied &= required
    missing = sorted(required - satisfied)
    # Filtered by the same set, for the same reason.  ``satisfied`` and
    # ``arrivals`` are two halves of one answer -- which upstreams reported,
    # and what they said -- so a name dropped from one has to leave the
    # other too.  Editing a graph is what pulls them apart: a step whose
    # upstreams changed keeps the old name's payload in this column, and
    # `_take_join` hands it to the run as something an upstream produced.
    # The step below would then be told about a report from a step that is
    # no longer above it, and read output from a round that is over.
    arrivals = {
        name: payload
        for name, payload in _join_arrivals(conn, task.id, workflow_run_id).items()
        if name in required
    }
    if emission.name in required:
        arrivals[emission.name] = dict(emission.payload)
    if workflow_run_id:
        conn.execute(
            """
                INSERT INTO workflow_signal_joins (
                    task_id, workflow_run_id, satisfied_json, arrivals_json, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(task_id, workflow_run_id) DO UPDATE SET
                    satisfied_json = excluded.satisfied_json,
                    arrivals_json = excluded.arrivals_json,
                    updated_at = excluded.updated_at
                """,
            (
                task.id,
                workflow_run_id,
                json.dumps(sorted(satisfied), ensure_ascii=False),
                json.dumps(arrivals, ensure_ascii=False),
                _iso(now),
            ),
        )
    else:
        conn.execute(
            """
                INSERT INTO signal_joins (
                    task_id, satisfied_json, arrivals_json, updated_at
                ) VALUES (?, ?, ?, ?)
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


def _join_arrivals(
    conn,
    task_id: str,
    workflow_run_id: str = "",
) -> dict[str, dict[str, Any]]:
    """The payloads a join has collected since its last completed round."""
    return txn_helpers._join_arrivals(
        conn,
        task_id=task_id,
        workflow_run_id=workflow_run_id,
    )


def _take_join(
    conn,
    task_id: str,
    workflow_run_id: str = "",
) -> dict[str, dict[str, Any]]:
    """Read a join's collected arrivals, then spend them.  Caller owns the txn.

        Read-and-delete in one call because the two are never wanted apart: an
        arrival that is read but not spent would let a stale payload describe a
        later round, and one spent without being read is exactly the loss this
        column exists to fix.
        """
    arrivals = _join_arrivals(conn, task_id, workflow_run_id)
    if workflow_run_id:
        conn.execute(
            "DELETE FROM workflow_signal_joins "
                "WHERE task_id = ? AND workflow_run_id = ?",
            (task_id, workflow_run_id),
        )
    else:
        conn.execute("DELETE FROM signal_joins WHERE task_id = ?", (task_id,))
    return arrivals


def _record_delivery(
    conn,
    emission_id: str,
    task_id: str,
    outcome: str,
    *,
    run_id: str = "",
    reason: str = "",
    now: datetime,
) -> None:
    txn_helpers._record_delivery(
        conn,
        emission_id=emission_id,
        task_id=task_id,
        outcome=outcome,
        run_id=run_id,
        reason=reason,
        now=now,
    )


def _pending_run_for(
    conn,
    task_id: str,
    workflow_run_id: str = "",
) -> Optional[str]:
    """The run already waiting for, or running, this task, if any."""
    return txn_helpers._pending_run_for(
        conn,
        task_id=task_id,
        workflow_run_id=workflow_run_id,
    )


def _round_waiting_on(
    conn,
    task: ScheduledTask,
) -> str:
    """The open round whose join is still short this task's success report.

        Used when a step is started by something other than an upstream signal
        -- a person pressing "run now", or a clock.  Neither has a parent run
        to inherit a round from, so without this the run mints a fresh
        workflow-run id: its success signal then carries that new id, the join
        that was waiting for it hears nothing, and the round it belongs to
        stalls for ever while an empty second round is created around the run.

        Only a round that is demonstrably waiting qualifies: some join in the
        same workflow has a pending arrival set that requires this task's
        success signal and does not have it yet.  Any other kind of round is
        left alone, so an ordinary scheduled entry still starts a new round of
        its own.
        """
    if not task.workflow_id:
        return ""
    wanted = task_signal_name(task.id, RUN_SUCCESS_STATUS)
    rows = conn.execute(
        """
            SELECT j.task_id, j.workflow_run_id, j.satisfied_json
            FROM workflow_signal_joins j
            JOIN scheduled_tasks t ON t.id = j.task_id
            JOIN workflow_runs w
              ON w.id = j.workflow_run_id AND w.status = 'running'
            WHERE t.workflow_id = ? AND t.enabled = 1
            ORDER BY j.updated_at DESC, j.rowid DESC
            """,
        (task.workflow_id,),
    ).fetchall()
    for row in rows:
        try:
            satisfied = {
                str(item)
                for item in json.loads(row["satisfied_json"] or "[]")
            }
        except (TypeError, ValueError):
            satisfied = set()
        if wanted in satisfied:
            continue
        join_task = get_task(conn, str(row["task_id"]))
        if join_task is None or join_task.trigger is None:
            continue
        if wanted in set(signal_names(join_task.trigger)):
            return str(row["workflow_run_id"])
    return ""


def _prepare_workflow_run(
    conn,
    task: ScheduledTask,
    run_id: str,
    snapshot: dict[str, Any],
    *,
    now: datetime,
    trigger_source: str,
) -> tuple[dict[str, Any], str]:
    """Attach a run to one durable workflow execution.

        A downstream signal carries the id created by the entry step.  A
        manual, scheduled or external-signal entry has no such parent, so its
        own run id becomes the workflow-run id.  The workflow graph is copied
        at that boundary; later edits therefore cannot rewrite what this
        execution means.
        """
    prepared = dict(snapshot or {})
    if not task.workflow_id:
        return prepared, ""
    inherited = str(prepared.get("workflow_run_id", "") or "").strip()
    signal = prepared.get("signal")
    if not inherited and isinstance(signal, dict):
        payload = signal.get("payload")
        if isinstance(payload, dict):
            same_workflow = str(payload.get("workflow_id", "") or "")
            if same_workflow == task.workflow_id:
                inherited = str(payload.get("workflow_run_id", "") or "").strip()
        # A free-form signal may start several entry branches without a
        # task payload.  Its cascade origin is the one shared durable id
        # all of those branches already carry, so it is also the natural
        # workflow execution id.
        if not inherited:
            inherited = str(signal.get("origin_id", "") or "").strip()
    # Nothing above names a parent for a step a person or a clock started,
    # but one may still be waiting: a join mid-round is short of exactly
    # this step's report.  Joining that round is what lets a manual run of
    # an upstream continue the chain instead of stranding it -- see
    # `_round_waiting_on`.  Retries are excluded: a retry's round comes
    # from the snapshot it was cloned with, and re-running with the
    # latest definition is a new attempt rather than a repair of the old
    # round.
    if not inherited and trigger_source in {"manual", "schedule"}:
        inherited = _round_waiting_on(conn, task)
    workflow_run_id = inherited or run_id
    workflow_row = conn.execute(
        "SELECT graph_json, version FROM workflows WHERE id = ?",
        (task.workflow_id,),
    ).fetchone()
    if workflow_row is None:
        return prepared, ""
    version = max(1, int(workflow_row["version"] or 1))
    workflow_meta = prepared.get("workflow")
    workflow_meta = dict(workflow_meta) if isinstance(workflow_meta, dict) else {}
    workflow_meta.update(
        {
            "workflow_id": task.workflow_id,
            "step_key": task.step_key,
            "workflow_run_id": workflow_run_id,
            "definition_version": version,
        }
    )
    prepared["workflow"] = workflow_meta
    prepared["workflow_run_id"] = workflow_run_id
    conn.execute(
        """
            INSERT OR IGNORE INTO workflow_runs (
                id, workflow_id, definition_version, definition_snapshot_json,
                trigger_source, root_task_id, root_run_id, status,
                created_at, updated_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, NULL)
            """,
        (
            workflow_run_id,
            task.workflow_id,
            version,
            str(workflow_row["graph_json"] or "{}"),
            trigger_source,
            task.id,
            run_id,
            _iso(now),
            _iso(now),
        ),
    )
    return prepared, workflow_run_id


def _enqueue_signal_run_in_transaction(
    conn,
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
    snapshot, workflow_run_id = _prepare_workflow_run(
        conn, task,
        run_id,
        snapshot,
        now=now,
        trigger_source=f"signal:{emission.name}",
    )
    conn.execute(
        """
            INSERT INTO scheduled_task_runs (
                id, task_id, scheduled_for, started_at, finished_at, status,
                summary, error, output_path, delivery_status,
                config_snapshot_json, trigger_source, attempt,
                workflow_run_id, missed_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, 'queued', '', '', '', '', ?,
                      ?, 1, ?, 0, ?, ?)
            """,
        (
            run_id,
            task.id,
            _iso(now),
            _iso(now),
            json.dumps(snapshot, ensure_ascii=False),
            f"signal:{emission.name}",
            workflow_run_id,
            _iso(now),
            _iso(now),
        ),
    )
    return run_id


def deliver_signals(
    conn,
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
    with _immediate_transaction(conn):
        rows = conn.execute(
            """
                SELECT * FROM signal_emissions
                WHERE state = 'pending'
                ORDER BY created_at ASC, rowid ASC
                LIMIT ?
                """,
            (max(1, int(limit)),),
        ).fetchall()
        for row in rows:
            emission = _emission_from_row(row)
            subscribers = _signal_subscribers(conn, emission.name)
            if not subscribers:
                _settle_emission(
                    conn, emission,
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
                    _record_delivery(
                        conn, emission.id,
                        task.id,
                        "refused",
                        reason=f"级联深度超过上限 {int(max_depth)}",
                        now=current,
                    )
                _settle_emission(
                    conn, emission,
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
                workflow_run_id = _emission_workflow_run_id(emission)
                if signal_mode(task.trigger) == SIGNAL_MODE_ALL:
                    # A join records this arrival and runs only when the
                    # last one shows up.  Landing here without a run is the
                    # normal case, not a failure, so it says so plainly
                    # instead of being filed under a state that means
                    # something else.  Recorded before the pending check
                    # below, because an arrival during a run belongs to the
                    # next round rather than to that run.
                    missing = _advance_join(conn, task, emission, current)
                    if missing:
                        _record_delivery(
                            conn, emission.id,
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
                    arrivals = _take_join(conn, task.id, workflow_run_id)
                pending = _pending_run_for(conn, task.id, workflow_run_id)
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
                    _record_delivery(
                        conn, emission.id,
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
                run_id = _enqueue_signal_run_in_transaction(
                    conn, task, emission, current, arrivals
                )
                _record_delivery(
                    conn, emission.id, task.id, "delivered", run_id=run_id, now=current
                )
                first_run_id = first_run_id or run_id
                delivered += 1
            if delivered:
                _settle_emission(
                    conn, emission,
                    state="delivered",
                    reason="",
                    run_id=first_run_id,
                    now=current,
                )
                tally["delivered"] += 1
            elif waiting:
                _settle_emission(
                    conn, emission,
                    state="waiting",
                    reason=(
                        f"{waiting} 个订阅任务在等其余上游信号，"
                            f"还在等：{'、'.join(dict.fromkeys(waiting_on))}"
                    ),
                    now=current,
                )
                tally["waiting"] += 1
            else:
                _settle_emission(
                    conn, emission,
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
    conn,
    emission: SignalEmission,
    *,
    state: str,
    reason: str,
    now: datetime,
    run_id: str = "",
) -> None:
    txn_helpers._settle_emission(
        conn,
        emission=emission,
        state=state,
        reason=reason,
        now=now,
        run_id=run_id,
    )


def _emit_run_signal(
    conn,
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
    row = conn.execute(
        "SELECT config_snapshot_json, workflow_run_id "
            "FROM scheduled_task_runs WHERE id = ?",
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
    workflow_snapshot = snapshot.get("workflow")
    workflow_snapshot = (
        workflow_snapshot if isinstance(workflow_snapshot, dict) else {}
    )
    task_row = conn.execute(
        "SELECT name, workflow_id, step_key, produces_json "
            "FROM scheduled_tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    # The run's own record of what it left behind, written by
    # ``complete_run`` earlier in this same transaction -- so what the
    # signal carries and what the run history says cannot disagree.
    products_row = conn.execute(
        "SELECT products_json FROM scheduled_task_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    payload = _run_report_payload(
        task_id=task_id,
        task_name=str(task_row["name"]) if task_row is not None else "",
        run_id=run_id,
        status=status,
        workflow_id=(
            str(workflow_snapshot.get("workflow_id", "") or "")
            or (str(task_row["workflow_id"] or "") if task_row is not None else "")
        ),
        step_key=(
            str(workflow_snapshot.get("step_key", "") or "")
            or (str(task_row["step_key"] or "") if task_row is not None else "")
        ),
        workflow_run_id=str(row["workflow_run_id"] or "")
        if row is not None
        else "",
        summary=summary,
        output_path=str(output_path or ""),
        declares=(
            snapshot.get("produces", [])
            if isinstance(snapshot.get("produces"), list)
            else (
                json.loads(task_row["produces_json"] or "[]")
                if task_row is not None
                else []
            )
        ),
        products=(
            json.loads(products_row["products_json"] or "[]")
            if products_row is not None
            else []
        ),
    )
    _insert_emission(
        conn, task_signal_name(task_id, status),
        payload,
        source="task",
        depth=int(parent.get("depth", 0) or 0) + 1 if parent else 0,
        origin_id=str(parent.get("origin_id", "") or "") if parent else "",
        now=finished_at,
    )


def claim_due_tasks(
    conn,
    now: datetime,
    limit: int = 10,
    lease_seconds: int = 300,
) -> list[ClaimedTask]:
    if lease_seconds < 3:
        raise ValueError("lease_seconds must be at least 3")
    now = now.astimezone(UTC)
    claimed: list[ClaimedTask] = []
    with _immediate_transaction(conn):
        _recover_stale_runs_in_transaction(conn, now)
        queued_rows = conn.execute(
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
            run = _run_from_row(queued_row)
            task_row = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ?", (run.task_id,)
            ).fetchone()
            if task_row is None:
                continue
            task = _task_from_row(task_row)
            lease_until = now + timedelta(seconds=lease_seconds)
            task_cursor = conn.execute(
                """
                    UPDATE scheduled_tasks
                    SET lease_until = ?, active_run_id = ?, updated_at = ?
                    WHERE id = ? AND enabled = 1 AND active_run_id IS NULL
                    """,
                (_iso(lease_until), run.id, _iso(now), task.id),
            )
            if task_cursor.rowcount != 1:
                continue
            conn.execute(
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
        rows = conn.execute(
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
            task = _task_from_row(row)
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
            snapshot, workflow_run_id = _prepare_workflow_run(
                conn, task,
                run_id,
                execution_snapshot(task),
                now=started_at,
                trigger_source="schedule",
            )
            conn.execute(
                """
                    INSERT INTO scheduled_task_runs (
                        id, task_id, scheduled_for, started_at, finished_at, status,
                        summary, error, output_path, delivery_status,
                        config_snapshot_json, trigger_source, attempt,
                        workflow_run_id, missed_count, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, 'running', '', '', '', '', ?, 'schedule', 1, ?, ?, ?, ?)
                    """,
                (
                    run_id,
                    task.id,
                    _iso(task.next_run_at),
                    _iso(started_at),
                    json.dumps(snapshot, ensure_ascii=False),
                    workflow_run_id,
                    int(missed_count),
                    _iso(started_at),
                    _iso(started_at),
                ),
            )
            cursor = conn.execute(
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
                conn.execute(
                    "DELETE FROM scheduled_task_runs WHERE id = ?", (run_id,)
                )
                continue
            refreshed_row = conn.execute(
                "SELECT * FROM scheduled_tasks WHERE id = ?", (task.id,)
            ).fetchone()
            refreshed = (
                _task_from_row(refreshed_row) if refreshed_row else None
            )
            assert refreshed is not None
            # Built by hand rather than re-read, so every field the row got
            # has to be repeated here: one that is left out silently reads
            # as its default while the database holds the real value, and
            # the run the scheduler is about to execute would disagree with
            # the run the history shows.  The snapshot is the *prepared*
            # one for the same reason -- `_prepare_workflow_run` just
            # stamped the workflow round into it, and the row above stores
            # that copy, not the raw `execution_snapshot`.
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
                        config_snapshot=snapshot,
                        trigger_source="schedule",
                        workflow_run_id=workflow_run_id,
                        missed_count=missed_count,
                        created_at=started_at,
                        updated_at=started_at,
                    ),
                )
            )
    return claimed


def _recover_stale_runs_in_transaction(
    conn,
    now: datetime,
) -> int:
    rows = conn.execute(
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
        cursor = conn.execute(
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
        conn.execute(
            """
                    UPDATE scheduled_task_runs
                    SET status = CASE
                            WHEN cancel_requested_at IS NOT NULL THEN 'cancelled'
                            ELSE 'interrupted'
                        END,
                        finished_at = ?, updated_at = ?
                    WHERE id = ? AND task_id = ? AND status = 'running'
                    """,
            (
                _iso(now),
                _iso(now),
                row["active_run_id"],
                row["task_id"],
            ),
        )
        # The recovered run may be the last thing its workflow round ever
        # gets -- the scheduler that owned it is gone -- so the round is
        # rolled up here rather than waiting for a completion that will
        # not come.  Without this, one crash left the round ``running``
        # for ever and ``update_workflow`` refused every later edit.
        _settle_workflow_run_of_run(conn, row["active_run_id"], now)
        recovered += 1
    return recovered


def _skip_blocked_steps_in_transaction(
    conn,
    workflow_id: str,
    step_key: str,
    status: str,
    finished_at: datetime,
    workflow_run_id: str = "",
    workflow_steps: Optional[list[WorkflowStep]] = None,
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
    workflow = get_workflow(conn, workflow_id)
    steps = workflow_steps or (workflow.steps if workflow is not None else [])
    if not steps:
        return []
    blocked_keys = workflow_downstream_steps(steps, step_key)
    if not blocked_keys:
        return []
    # Cancel is the user stopping the work; the scheduler losing it
    # (interrupted) is nobody's failure either.  Both still mean the
    # downstream steps will not run, so they are recorded -- but with a
    # reason that says what happened rather than one that reads as the
    # chain having broken.
    if status == "cancelled":
        reason = (
            f"上游步骤「{step_key}」被取消，这一步不再等待它的成功信号，"
                "因此没有运行"
        )
    elif status == "interrupted":
        reason = (
            f"上游步骤「{step_key}」被中断（持有它的调度器已停止），"
                "这一步等不到它的成功信号，因此没有运行"
        )
    else:
        reason = (
            f"上游步骤「{step_key}」以 {status} 结束，这一步等不到它的成功信号，"
                "因此没有运行"
        )
    skipped: list[str] = []
    for step in steps:
        key = str(step.key).strip()
        if key not in blocked_keys:
            continue
        row = conn.execute(
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
        if workflow_run_id:
            existing = conn.execute(
                """
                    SELECT id FROM scheduled_task_runs
                    WHERE task_id = ? AND workflow_run_id = ?
                    LIMIT 1
                    """,
                (blocked_task_id, workflow_run_id),
            ).fetchone()
            if existing is not None:
                continue
        if _pending_run_for(conn, blocked_task_id, workflow_run_id):
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
        conn.execute(
            """
                INSERT INTO scheduled_task_runs (
                    id, task_id, scheduled_for, started_at, finished_at, status,
                    summary, error, output_path, delivery_status,
                    config_snapshot_json, trigger_source, attempt,
                    workflow_run_id, missed_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '', '', '', ?, ?, 1, ?, 0, ?, ?)
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
                workflow_run_id,
                _iso(finished_at),
                _iso(finished_at),
            ),
        )
        # ``last_run_at`` is deliberately not touched.  It answers "when did
        # this task last run", and a skipped task has never run -- its never
        # having run is the whole content of this record.  A step that looks
        # like it ran is the failure this row exists to prevent.
        _emit_run_signal(
            conn, blocked_task_id,
            run_id,
            RUN_SKIPPED_STATUS,
            finished_at,
            reason,
        )
        skipped.append(blocked_task_id)
    return skipped


def _workflow_run_steps(
    conn,
    workflow_run_id: str,
) -> list[WorkflowStep]:
    return txn_helpers._workflow_run_steps(
        conn,
        workflow_run_id=workflow_run_id,
    )


def _settle_workflow_run(
    conn,
    workflow_run_id: str,
    now: datetime,
) -> None:
    """Roll up a workflow execution once nothing of it is still in flight.

        A step can still run only while it, or something of the round it
        belongs to, has a run queued or running.  The ``expected`` set starts
        from the steps that have already appeared and grows by their
        downstreams, so a step that has not appeared yet keeps the round open
        -- it may still be fed, by the upstream that owes it a report or by a
        manual run inheriting this round (see :meth:`_round_waiting_on`).

        A round that stays open that way has nothing in flight and cannot
        finish on its own; it is closed by :meth:`_close_rounds_before_edit`
        when the definition is next edited, rather than being declared over
        here where it would also lose the chance of that manual rescue.

        Caller owns the transaction.
        """
    if not workflow_run_id:
        return
    definition_steps = _workflow_run_steps(conn, workflow_run_id)
    if not definition_steps:
        return
    rows = conn.execute(
        """
            SELECT r.status, t.step_key
            FROM scheduled_task_runs r
            JOIN scheduled_tasks t ON t.id = r.task_id
            WHERE r.workflow_run_id = ? AND t.step_key != ''
            ORDER BY r.created_at DESC, r.rowid DESC
            """,
        (workflow_run_id,),
    ).fetchall()
    latest: dict[str, str] = {}
    for row in rows:
        latest.setdefault(str(row["step_key"]), str(row["status"]))
    if not latest:
        return
    expected: set[str] = set(latest)
    for step_key in tuple(latest):
        expected.update(workflow_downstream_steps(definition_steps, step_key))
    if any(key not in latest for key in expected):
        return
    if any(latest[key] in {"queued", "running"} for key in expected):
        return
    # The round's outcome is its worst step's, ranked by what a reader
    # needs to know: a failure outranks everything, because something the
    # chain was for did not happen.  Cancel is the user stopping the work
    # and interrupted is the scheduler losing it; neither is a failure, and
    # recording either as ``failed`` would turn a decision somebody made
    # into history that says "it broke".  ``skipped`` alone cannot happen
    # in a round that did its work -- a skipped step is only ever written
    # beneath a step that did not succeed -- so a round of nothing but
    # skips reads as failed.
    outcomes = {latest[key] for key in expected}
    if outcomes == {RUN_SUCCESS_STATUS}:
        status = RUN_SUCCESS_STATUS
    elif "failed" in outcomes:
        status = "failed"
    elif "cancelled" in outcomes:
        status = "cancelled"
    elif "interrupted" in outcomes:
        status = "interrupted"
    elif RUN_UNVERIFIED_STATUS in outcomes:
        status = RUN_UNVERIFIED_STATUS
    else:
        status = "failed"
    conn.execute(
        """
            UPDATE workflow_runs
            SET status = ?, updated_at = ?, finished_at = ?
            WHERE id = ? AND status = 'running'
            """,
        (status, _iso(now), _iso(now), workflow_run_id),
    )


def _settle_workflow_run_of_run(
    conn,
    run_id: str,
    now: datetime,
) -> None:
    """Close out the workflow round one run belonged to.  Caller owns txn.

        Every path that ends a run without completing it -- lease recovery,
        claim release -- reaches here, and each of them can be the last event
        the round ever gets: the scheduler that owned the run is gone, so no
        completion and no success signal is coming.  The downstream steps the
        round was waiting on are therefore recorded as skipped (the same
        record ``complete_run`` writes for a failure, with a reason that says
        which non-success ending caused it), and the round is rolled up.
        """
    row = conn.execute(
        """
            SELECT r.task_id, r.status, r.workflow_run_id,
                   t.workflow_id, t.step_key
            FROM scheduled_task_runs r
            JOIN scheduled_tasks t ON t.id = r.task_id
            WHERE r.id = ?
            """,
        (run_id,),
    ).fetchone()
    if row is None:
        return
    workflow_run_id = str(row["workflow_run_id"] or "")
    if not workflow_run_id:
        return
    status = str(row["status"] or "")
    if status and status != RUN_SUCCESS_STATUS:
        _skip_blocked_steps_in_transaction(
            conn, str(row["workflow_id"] or ""),
            str(row["step_key"] or ""),
            status,
            now,
            workflow_run_id,
            _workflow_run_steps(conn, workflow_run_id),
        )
    _settle_workflow_run(conn, workflow_run_id, now)


def complete_run(
    conn,
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
    finished_at = finished_at.astimezone(UTC)
    retry_at = retry_at.astimezone(UTC) if retry_at is not None else None
    # The status decides what the record means and what every subscriber
    # hears, and this method is called from several modules.  A typo used
    # to be absorbed silently -- anything that was not `succeeded` was
    # treated as a failure, so `success` marked the whole downstream chain
    # skipped and the workflow run failed, with nothing saying why.  The
    # legal vocabulary is known here, so it is checked here.
    if status not in TERMINAL_RUN_STATUSES:
        raise ValueError(
            f"未知的运行终态 {status!r}；可用："
            + "、".join(TERMINAL_RUN_STATUSES)
        )
    with _immediate_transaction(conn):
        task_cursor = conn.execute(
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
        run_cursor = conn.execute(
            """
                UPDATE scheduled_task_runs
                SET status = ?, summary = ?, error = ?, output_path = ?,
                    delivery_status = ?, verdict = ?, verification_json = ?,
                    products_json = ?,
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
                json.dumps(products_payload(products), ensure_ascii=False),
                _iso(finished_at),
                _iso(finished_at),
                run_id,
                task_id,
            ),
        )
        if run_cursor.rowcount != 1:
            raise RuntimeError("owned scheduler run disappeared during completion")
        retry_queued = False
        if retry_at is not None and status in RETRYABLE_RUN_STATUSES:
            source_row = conn.execute(
                "SELECT * FROM scheduled_task_runs WHERE id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()
            if source_row is None:
                raise RuntimeError(
                    "completed scheduler run disappeared before retry"
                )
            source = _run_from_row(source_row)
            retry_id = _new_id()
            created_at = datetime.now(UTC)
            conn.execute(
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
                    retry_id,
                    task_id,
                    _iso(source.scheduled_for),
                    _iso(retry_at),
                    json.dumps(source.config_snapshot, ensure_ascii=False),
                    source.attempt + 1,
                    run_id,
                    source.workflow_run_id,
                    _iso(created_at),
                    _iso(created_at),
                ),
            )
            retry_queued = True
        # Inside the same transaction as the status, deliberately.  If the
        # two could commit separately, a crash between them would leave a
        # task that visibly succeeded while whatever was waiting on it
        # waits forever -- the exact failure that is hardest to notice.
        # A failed attempt with a durable retry queued is not the task's
        # terminal outcome.  Publishing it would make subscribers and
        # workflow descendants act on a failure that the retry policy says
        # is still in progress.
        if not retry_queued:
            _emit_run_signal(
                conn, task_id, run_id, status, finished_at, summary, output_path
            )
        # Same reasoning, one step further: a step that did not do its work
        # cannot unblock the steps below it, and their run will never
        # happen.  Recorded here, with the status, so no control path can
        # finish a step without the steps it blocks being settled.
        membership = conn.execute(
            """
                SELECT t.workflow_id, t.step_key, r.workflow_run_id
                FROM scheduled_tasks t
                JOIN scheduled_task_runs r ON r.id = ? AND r.task_id = t.id
                WHERE t.id = ?
                """,
            (run_id, task_id),
        ).fetchone()
        if (
            not retry_queued
            and membership is not None
            and membership["workflow_id"]
        ):
            workflow_run_id = str(membership["workflow_run_id"] or "")
            _skip_blocked_steps_in_transaction(
                conn, membership["workflow_id"],
                str(membership["step_key"] or ""),
                status,
                finished_at,
                workflow_run_id,
                _workflow_run_steps(conn, workflow_run_id),
            )
        _settle_workflow_run(
            conn, str(membership["workflow_run_id"] or "") if membership else "",
            finished_at,
        )
    return True


def get_workflow(
    conn,
    workflow_id: str,
) -> Optional[Workflow]:
    row = conn.execute(
        "SELECT * FROM workflows WHERE id = ? LIMIT 1", (workflow_id,)
    ).fetchone()
    return _workflow_from_row(row) if row else None

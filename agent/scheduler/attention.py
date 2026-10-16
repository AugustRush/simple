"""What needs attention: the snapshot the dashboard reads and the upstream reports a run depends on. Moved out of SchedulerStore (see the remaining-big-files survey work order)."""

from __future__ import annotations
import json

from agent.scheduler.rows import _attention_clause, _run_from_row
from agent.scheduler.workflow_engine import _run_report_payload
from .models import ScheduledTask, parse_task_signal, signal_names

from typing import Any

__all__ = [
    "attention_snapshot",
    "_upstream_reports_in_transaction",
]


def attention_snapshot(
    conn,
    limit: int = 200,
) -> dict[str, Any]:
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
    per_task = conn.execute(
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
    rows = conn.execute(
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


def _upstream_reports_in_transaction(
    conn,
    task: ScheduledTask,
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
        upstream = conn.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ?", (upstream_id,)
        ).fetchone()
        if upstream is None:
            continue
        latest = conn.execute(
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


def attention_snapshot(
    conn,
    limit: int = 200,
) -> dict[str, Any]:
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
    per_task = conn.execute(
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
    rows = conn.execute(
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


def _upstream_reports_in_transaction(
    conn,
    task: ScheduledTask,
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
        upstream = conn.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ?", (upstream_id,)
        ).fetchone()
        if upstream is None:
            continue
        latest = conn.execute(
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

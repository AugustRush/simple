"""Retention, decay, and clearing persistent state. Moved out of LTMStore (see the remaining-big-files survey work order). The cache invalidation the class used to do itself arrives as the invalidate collaborator, because a derived cache is instance state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from agent import shared
from . import fts
from . import _helpers
from ._helpers import _now

__all__ = [
    "apply_retention",
    "apply_decay",
    "clear_persistent_memory",
]


def clear_persistent_memory(
    conn,
    *,
    invalidate,
) -> dict[str, int]:
    """Atomically delete all model-retrievable memory state."""
    tables = (
        "memory_items_fts",
        "memory_items",
        "fact_assertions",
        "resolved_facts",
        "conversation_turns",
        "session_working_state",
        "agent_events",
        "staging_turns",
    )
    deleted: dict[str, int] = {}
    existing = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    for table in tables:
        if table not in existing:
            deleted[table] = 0
            continue
        row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        deleted[table] = int(row["count"] if row else 0)
        conn.execute(f"DELETE FROM {table}")
    invalidate(set())
    return deleted


def apply_decay(
    conn,
    factor: float = shared.DECAY_FACTOR,
    *,
    invalidate,
) -> None:
    """Decay importance of all entries; prune those below shared.MIN_IMPORTANCE.

        Pushed into SQL so it no longer fetches every entry into Python and
        writes them back one row at a time (the old per-row _write_entry_row
        also re-synced each entry's FTS row individually).  Mirrors
        apply_retention, which already does this for episodes.
        """
    now = _now()
    conn.execute(
        """
                UPDATE memory_items
                SET importance = importance * ?,
                    updated_at = ?
                WHERE status NOT IN ('archived', 'superseded')
                """,
        (factor, now),
    )
    pruned = [
        row["id"]
        for row in conn.execute(
            """
                    SELECT id FROM memory_items
                    WHERE status NOT IN ('archived', 'superseded')
                      AND importance < ?
                    """,
            (shared.MIN_IMPORTANCE,),
        ).fetchall()
    ]
    if pruned:
        fts._delete_fts_rows(conn, pruned)
        conn.execute(
            f"DELETE FROM memory_items WHERE id IN ({','.join('?' for _ in pruned)})",
            pruned,
        )
    invalidate(set())


def apply_retention(
    conn,
    *,
    invalidate,
) -> None:
    """Apply locus-aware retention: decay episodes in-database, leave others untouched.

        Previous implementation fetched ALL active rows into Python just to
        skip ~90% of them.  This version pushes the work into SQL, touching
        only the rows that need to change.
        """
    now = _now()
    run_cutoff = (
        datetime.now(timezone.utc) - timedelta(days=_helpers._RUN_SCRATCH_RETENTION_DAYS)
    ).strftime("%Y-%m-%d %H:%M UTC")
    usage_cutoff = (
        datetime.now(timezone.utc) - timedelta(days=_helpers._USAGE_EVENT_RETENTION_DAYS)
    ).strftime("%Y-%m-%d %H:%M:%S.%f UTC")
    # Step 1: decay importance for all active episodes in a single UPDATE.
    # ``pinned = 0`` is the whole promise of the pin: an entry the operator
    # chose to keep is not decayed, so it can never fall through the importance
    # floor in step 2 either.  Every step below repeats the clause rather than
    # relying on that, because the run-scratch prunes select on age and row
    # count, neither of which a pin affects.
    conn.execute(
        """
                UPDATE memory_items
                SET importance = importance * ?,
                    updated_at  = ?
                WHERE category = 'episodes'
                  AND status NOT IN ('archived', 'superseded')
                  AND pinned = 0
                """,
        (shared.DECAY_FACTOR, now),
    )
    # Step 2: archive episodes that fell below the importance floor.
    archived_ids = {
        row["id"]
        for row in conn.execute(
            """
                    SELECT id FROM memory_items
                    WHERE category = 'episodes'
                      AND importance < ?
                      AND status NOT IN ('archived', 'superseded')
                      AND pinned = 0
                    """,
            (shared.MIN_IMPORTANCE,),
        ).fetchall()
    }
    archived_ids.update(
        row["id"]
        for row in conn.execute(
            """
                    SELECT id FROM memory_items
                    WHERE category = 'episodes'
                      AND scope LIKE 'run:%'
                      AND created_at < ?
                      AND status NOT IN ('archived', 'superseded')
                      AND pinned = 0
                    """,
            (run_cutoff,),
        ).fetchall()
    )
    archived_ids.update(
        row["id"]
        for row in conn.execute(
            """
                    SELECT id FROM memory_items
                    WHERE category = 'episodes'
                      AND scope LIKE 'run:%'
                      AND status NOT IN ('archived', 'superseded')
                      AND pinned = 0
                    ORDER BY created_at DESC, id DESC
                    LIMIT -1 OFFSET ?
                    """,
            (_helpers._RUN_SCRATCH_MAX_ACTIVE,),
        ).fetchall()
    )
    if archived_ids:
        archived_id_list = sorted(archived_ids)
        conn.execute(
            f"""
                    UPDATE memory_items
                    SET status     = 'archived',
                        updated_at = ?
                    WHERE id IN ({",".join("?" for _ in archived_id_list)})
                    """,
            (now, *archived_id_list),
        )
        fts._delete_fts_rows(conn, archived_id_list)
    # The usage ledger is append-only by design (one row per provider
    # call); bound it here so a long-running gateway cannot grow the
    # table without limit. created_at is fixed-width UTC, so the
    # lexicographic comparison tracks time order.
    conn.execute(
        "DELETE FROM usage_events WHERE created_at < ?",
        (usage_cutoff,),
    )
    invalidate({"episodes"})

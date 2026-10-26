"""Schema steps an existing ``LTMStore`` database may still need.

Extracted from ``LTMStore`` (see the remaining-big-files survey): each takes the connection and
nothing else, which is what makes a migration a thing that can be read in order and tested
against a copied database rather than against a store.

Imported back into ``agent/memory/store.py``, so ``_ensure_schema`` -- the only caller -- keeps
its sequence and nothing about the on-disk format changes.
"""

from __future__ import annotations

import sqlite3

__all__ = [
    "_ensure_transcript_indexes",
    "_add_missing_columns",
    "_backfill_transcript_seq",
    "_next_transcript_seq",
]


def _ensure_transcript_indexes(conn: sqlite3.Connection) -> None:
    """Indexes over the transcript sequence.

    Split out of the schema script and run only once the ``seq`` columns
    exist. Declaring them inline looked equivalent but was not: on a
    database whose tables predate the column, the index statement is the
    first thing to mention it, and it fails outright.
    """
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversation_turns_session_seq"
        " ON conversation_turns(session_id, seq)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_events_session_seq"
        " ON agent_events(session_id, seq)"
    )


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current column set.

    ``CREATE TABLE IF NOT EXISTS`` is a no-op on a table that already
    exists, so a database written before a column was introduced never
    receives it. Reading that column then raises, which turns a schema
    change into a broken store rather than a migrated one.
    """
    for table, column, ddl in (
        ("conversation_turns", "seq", "INTEGER NOT NULL DEFAULT 0"),
        ("agent_events", "seq", "INTEGER NOT NULL DEFAULT 0"),
        # ``pinned`` exempts an entry from retention.  Adding it here rather
        # than only to ``_ensure_schema`` is what keeps an existing palace.db
        # readable: the CREATE TABLE is a no-op on a file that already has the
        # table, so without this the first read of the column would raise on
        # the exact databases that hold someone's memories.
        ("memory_items", "pinned", "INTEGER NOT NULL DEFAULT 0"),
    ):
        existing = {
            str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if not existing:
            # The table itself is missing (a fresh database), so the schema
            # script already created it with every column.
            continue
        if column in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _backfill_transcript_seq(conn: sqlite3.Connection) -> None:
    """Give pre-existing rows a sequence, ordered by when they were written.

    Rows written before ``seq`` existed are ordered by timestamp and then by
    row id, interleaving both tables. That is the best reconstruction
    available from what those rows recorded, and it keeps a reader from
    being told an old transcript is in an order it never was in. Rows that
    already carry a sequence are left alone, so this is safe to re-run.
    """
    sessions = [
        str(row["session_id"])
        for row in conn.execute(
            """
            SELECT DISTINCT session_id FROM conversation_turns WHERE seq = 0
            UNION
            SELECT DISTINCT session_id FROM agent_events WHERE seq = 0
            """
        ).fetchall()
    ]
    for session_id in sessions:
        rows = conn.execute(
            """
            SELECT 'turn' AS source, id, created_at FROM conversation_turns
            WHERE session_id = ? AND seq = 0
            UNION ALL
            SELECT 'event' AS source, id, created_at FROM agent_events
            WHERE session_id = ? AND seq = 0
            """,
            (session_id, session_id),
        ).fetchall()
        # A turn row is written before the events it produced when they
        # share a timestamp, and after them otherwise -- which the sort
        # below reproduces by ordering on time first and source second only
        # to break exact ties.
        ordered = sorted(rows, key=lambda row: (str(row["created_at"]), str(row["source"]), int(row["id"])))
        seq = 1
        for row in ordered:
            table = (
                "conversation_turns" if str(row["source"]) == "turn" else "agent_events"
            )
            conn.execute(f"UPDATE {table} SET seq = ? WHERE id = ?", (seq, int(row["id"])))
            seq += 1
        if seq > 1:
            conn.execute(
                """
                INSERT INTO transcript_seq_counters (session_id, next_seq)
                VALUES (?, ?)
                ON CONFLICT(session_id) DO UPDATE SET next_seq = excluded.next_seq
                """,
                (session_id, seq),
            )


def _next_transcript_seq(conn: sqlite3.Connection, session_id: str) -> int:
    """Reserve the next position in ``session_id``'s single ordered log.

    Both ``conversation_turns`` and ``agent_events`` draw from this one
    counter, which is what makes a transcript totally ordered: a tool call
    and the reply it fed have one shared timeline rather than two tables'
    independent autoincrement ids, which are not comparable. The reservation
    is an upsert that reads back its own result, so concurrent writers each
    get a distinct position without a separate round trip.
    """
    row = conn.execute(
        """
        INSERT INTO transcript_seq_counters (session_id, next_seq)
        VALUES (?, 2)
        ON CONFLICT(session_id) DO UPDATE SET next_seq = next_seq + 1
        RETURNING next_seq
        """,
        (session_id,),
    ).fetchone()
    return int(row["next_seq"]) - 1 if row else 1

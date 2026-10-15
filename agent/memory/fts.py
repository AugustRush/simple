"""Keeping the FTS mirror in step with the rows it indexes.

Three functions moved out of ``LTMStore`` (see the remaining-big-files survey).  Each already
took the connection as a parameter and used nothing else off the store -- they only *looked*
bound because they were methods.  The full-text index is a second copy of data that lives in
``entries``, so every write path here is about keeping the copy honest, and the category list
is derived from the same rows rather than stored.
"""

from __future__ import annotations

import sqlite3

from agent import shared

__all__ = [
    "_delete_fts_rows",
    "_dynamic_category_names",
    "_sync_fts_row",
]


def _delete_fts_rows(
    conn: sqlite3.Connection,
    entry_ids: list[str] | set[str] | tuple[str, ...],
) -> None:
    ids = [entry_id for entry_id in entry_ids if entry_id]
    if not ids:
        return
    conn.execute(
        f"DELETE FROM memory_items_fts WHERE memory_id IN ({','.join('?' for _ in ids)})",
        ids,
    )


def _sync_fts_row(
    conn: sqlite3.Connection,
    entry_id: str,
) -> None:
    _delete_fts_rows(conn, [entry_id])
    row = conn.execute(
        """
        SELECT id, content, entity, category, status
        FROM memory_items
        WHERE id = ?
        LIMIT 1
        """,
        (entry_id,),
    ).fetchone()
    if row is None or row["status"] in {"archived", "superseded"}:
        return
    conn.execute(
        """
        INSERT INTO memory_items_fts (memory_id, content, entity, category)
        VALUES (?, ?, ?, ?)
        """,
        (row["id"], row["content"], row["entity"], row["category"]),
    )


def _dynamic_category_names(
    conn: sqlite3.Connection,
) -> set[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT category FROM memory_items
        WHERE status = 'active'
        """
    ).fetchall()
    return {row["category"] for row in rows if row["category"] not in shared.PALACE_LOCI}

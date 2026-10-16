""""""

from __future__ import annotations
import sqlite3

from . import rows as _row_mappers, normalize
from .models import LTMEntry

from typing import Any, Optional

__all__ = [
    "read_entries",
    "read_entries_for_entity",
    "read_manual_note",
    "all_entries",
    "_find_existing_entry_row",
    "_match_existing_entry_id",
]


def _match_existing_entry_id(
    conn: sqlite3.Connection,
    entry: LTMEntry,
) -> Optional[str]:
    merge_key = normalize._stable_merge_key(entry)
    if not merge_key:
        return None

    category = normalize.normalize_category_name(entry.category)
    entity = normalize._normalize_entity(entry.entity, category)
    memory_type = (entry.memory_type or "fact").strip().lower()

    normalized_content = normalize._normalize_content_key(entry.content)
    rows = conn.execute(
        """
            SELECT * FROM memory_items
            WHERE category = ? AND entity = ? AND memory_type = ?
              AND status NOT IN ('archived', 'superseded')
            ORDER BY updated_at DESC, id ASC
            """,
        (category, entity, memory_type),
    ).fetchall()
    for row in rows:
        if normalize._normalize_content_key(row["content"]) == normalized_content:
            return row["id"]
    return None


def _find_existing_entry_row(
    conn: sqlite3.Connection,
    entry: LTMEntry,
) -> Optional[sqlite3.Row]:
    row = conn.execute(
        "SELECT * FROM memory_items WHERE id = ? LIMIT 1",
        (entry.id,),
    ).fetchone()
    if row:
        return row
    existing_id = _match_existing_entry_id(conn, entry)
    if not existing_id:
        return None
    return conn.execute(
        "SELECT * FROM memory_items WHERE id = ? LIMIT 1",
        (existing_id,),
    ).fetchone()


def read_entries(
    conn,
    category: str,
    *,
    scopes: Optional[list[str]] = None,
) -> list[LTMEntry]:
    if scopes is not None and not scopes:
        return []
    category = normalize.normalize_category_name(category)
    sql = """
                SELECT * FROM memory_items
                WHERE category = ? AND status NOT IN ('archived', 'superseded')
            """
    params: list[Any] = [category]
    if scopes:
        sql += f" AND scope IN ({','.join('?' for _ in scopes)})"
        params.extend(str(scope) for scope in scopes)
    sql += " ORDER BY importance DESC, updated_at DESC, id ASC"
    rows = conn.execute(sql, params).fetchall()
    return [_row_mappers._row_to_entry(row) for row in rows]


def read_entries_for_entity(
    conn,
    category: str,
    entity: str,
) -> list[LTMEntry]:
    category = normalize.normalize_category_name(category)
    entity = normalize._normalize_entity(entity, category)
    rows = conn.execute(
        """
                SELECT * FROM memory_items
                WHERE category = ? AND entity = ?
                  AND status NOT IN ('archived', 'superseded')
                ORDER BY importance DESC, updated_at DESC, id ASC
                """,
        (category, entity),
    ).fetchall()
    return [_row_mappers._row_to_entry(row) for row in rows]


def read_manual_note(
    conn,
    category: str,
    entity: str,
) -> Optional[LTMEntry]:
    category = normalize.normalize_category_name(category)
    entity = normalize._normalize_entity(entity, category)
    row = conn.execute(
        """
                SELECT * FROM memory_items
                WHERE category = ? AND entity = ? AND memory_type = 'note'
                  AND status NOT IN ('archived', 'superseded')
                ORDER BY updated_at DESC, id ASC
                LIMIT 1
                """,
        (category, entity),
    ).fetchone()
    return _row_mappers._row_to_entry(row) if row else None


def all_entries(conn) -> list[LTMEntry]:
    rows = conn.execute(
        """
                SELECT * FROM memory_items
                WHERE status NOT IN ('archived', 'superseded')
                ORDER BY importance DESC, updated_at DESC, id ASC
                """
    ).fetchall()
    return [_row_mappers._row_to_entry(row) for row in rows]

"""Writing entries: the row-level helpers and the entry writers. Moved out of LTMStore (see the remaining-big-files survey work order). The category cap and the cache invalidation the class owned arrive as collaborators."""

from __future__ import annotations
import sqlite3

from agent import shared
from ._helpers import _new_id, _now
from .models import LTMEntry
from . import fts, normalize, rows as _row_mappers, entries, facts
from .rows import _row_flag

__all__ = [
    "_coerce_category_for_storage",
    "_write_entry_row",
    "write_entries",
    "add_entry",
    "add_entries",
    "upsert_manual_note",
    "_remove_category",
    "merge_categories",
]


def _coerce_category_for_storage(
    conn: sqlite3.Connection,
    category: str,
    *,
    max_categories,
) -> str:
    normalized = normalize.normalize_category_name(category)
    if normalized in shared.PALACE_LOCI:
        return normalized
    dynamic_categories = fts._dynamic_category_names(conn)
    if normalized in dynamic_categories:
        return normalized
    if len(dynamic_categories) < max_categories:
        return normalized
    return "concepts"


def _write_entry_row(
    conn: sqlite3.Connection,
    entry: LTMEntry,
    *,
    invalidate,
    max_categories,
) -> set[str]:
    original_category = normalize.normalize_category_name(entry.category)
    original_entity = str(entry.entity or "")
    entry.category = _coerce_category_for_storage(conn, original_category, max_categories=max_categories)
    if entry.category == "concepts" and original_category not in shared.PALACE_LOCI:
        entry.entity = (
            normalize.normalize_category_name(original_entity)
            if original_entity.strip()
            else original_category
        )
    entry.category = normalize.normalize_category_name(entry.category)
    entry.entity = normalize._normalize_entity(entry.entity, entry.category)
    entry.memory_type = entry.memory_type or "fact"
    entry.scope = entry.scope or "global"
    entry.status = entry.status or "active"
    entry.source_session = entry.source_session or ""
    entry.confidence = float(entry.confidence or 1.0)
    existing_row = entries._find_existing_entry_row(conn, entry)
    affected_categories = {entry.category}
    if existing_row:
        entry.id = existing_row["id"]
        entry.created_at = existing_row["created_at"]
        # A pin is the operator's decision, not a property of the prose.  The
        # replace below rewrites the whole row, so carrying it here is what
        # stops a consolidation pass that re-states a pinned entry from quietly
        # unpinning it -- the pin would then vanish on the next retention sweep
        # while the page still showed it as pinned a moment earlier.
        entry.pinned = _row_flag(existing_row, "pinned")
        affected_categories.add(
            normalize.normalize_category_name(existing_row["category"])
        )
    # ``ON CONFLICT ... DO UPDATE`` rather than ``INSERT OR REPLACE``.  REPLACE
    # is a delete followed by an insert, so every column *not* named in this
    # statement is reset to its default on each rewrite -- which is exactly how
    # ``pinned`` was silently unpinned by the next consolidation pass, and how
    # the next column added to this table would be lost the same way.  Naming
    # the mutable columns here makes the writer responsible only for what it
    # means to overwrite.
    conn.execute(
        """
            INSERT INTO memory_items (
                id, content, importance, category, entity, memory_type, scope,
                status, source_session, confidence, pinned, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                content = excluded.content,
                importance = excluded.importance,
                category = excluded.category,
                entity = excluded.entity,
                memory_type = excluded.memory_type,
                scope = excluded.scope,
                status = excluded.status,
                source_session = excluded.source_session,
                confidence = excluded.confidence,
                pinned = excluded.pinned,
                updated_at = excluded.updated_at
            """,
        (
            entry.id,
            entry.content,
            float(entry.importance),
            entry.category,
            entry.entity,
            entry.memory_type,
            entry.scope,
            entry.status,
            entry.source_session,
            float(entry.confidence),
            int(entry.pinned or 0),
            entry.created_at,
            entry.updated_at,
        ),
    )
    fts._sync_fts_row(conn, entry.id)
    return {
        normalize.normalize_category_name(category) for category in affected_categories
    }


def _remove_category(
    conn,
    category: str,
    *,
    invalidate,
    max_categories,
) -> None:
    category = normalize.normalize_category_name(category)
    row_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM memory_items WHERE category = ?",
            (category,),
        ).fetchall()
    ]
    conn.execute("DELETE FROM memory_items WHERE category = ?", (category,))
    fts._delete_fts_rows(conn, row_ids)
    invalidate({category})


def write_entries(
    conn,
    category: str,
    entries: list[LTMEntry],
    *,
    invalidate,
    max_categories,
) -> None:
    category = normalize.normalize_category_name(category)
    if not entries:
        _remove_category(conn, category, invalidate=invalidate, max_categories=max_categories)
        return

    affected_categories = {category}
    row_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM memory_items WHERE category = ?",
            (category,),
        ).fetchall()
    ]
    conn.execute("DELETE FROM memory_items WHERE category = ?", (category,))
    fts._delete_fts_rows(conn, row_ids)
    for entry in entries:
        entry.category = category
        affected_categories.update(_write_entry_row(conn, entry, invalidate=invalidate, max_categories=max_categories))
    invalidate(affected_categories)


def add_entry(
    conn,
    entry: LTMEntry,
    *,
    invalidate,
    max_categories,
) -> None:
    entry.category = normalize.normalize_category_name(entry.category)
    entry.entity = normalize._normalize_entity(entry.entity, entry.category)
    affected_categories = _write_entry_row(conn, entry, invalidate=invalidate, max_categories=max_categories)
    invalidate(affected_categories)
    for fact in facts._fact_assertions_from_entry(conn, entry):
        facts.add_fact_assertion(conn, fact)


def add_entries(
    conn,
    entries: list[LTMEntry],
    *,
    invalidate,
    max_categories,
) -> None:
    """Batch-insert multiple entries in a single transaction and one sync pass.

        Preferred over calling add_entry() in a loop: consolidation writes all
        extracted facts at once, avoiding N separate SQL transactions and N
        rounds of _meta + snapshot + projection file I/O.
        """
    if not entries:
        return
    affected_categories: set[str] = set()
    for entry in entries:
        entry.category = normalize.normalize_category_name(entry.category)
        entry.entity = normalize._normalize_entity(entry.entity, entry.category)
        affected_categories.update(_write_entry_row(conn, entry, invalidate=invalidate, max_categories=max_categories))
    invalidate(affected_categories)
    for entry in entries:
        for fact in facts._fact_assertions_from_entry(conn, entry):
            facts.add_fact_assertion(conn, fact)


def upsert_manual_note(
    conn,
    category: str,
    entity: str,
    content: str,
    append: bool = False,
    *,
    invalidate,
    max_categories,
) -> LTMEntry:
    category = normalize.normalize_category_name(category)
    entity = normalize._normalize_entity(entity, category)
    clean_content = str(content or "").strip()
    # Read-modify-write under one transaction: a concurrent append
    # must not read the same base note and overwrite the other
    # writer's update (lost update).
    conn.execute("BEGIN IMMEDIATE")
    try:
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
        if row:
            entry = _row_mappers._row_to_entry(row)
            entry.content = (
                f"{entry.content.rstrip()}\n{clean_content}"
                if append
                else clean_content
            ).strip()
            entry.updated_at = _now()
        else:
            entry = LTMEntry(
                id=_new_id(),
                content=clean_content,
                importance=0.8,
                category=category,
                entity=entity,
                memory_type="note",
                scope="global",
                status="active",
                source_session="manual_memory_write",
                confidence=1.0,
                created_at=_now(),
                updated_at=_now(),
            )
        affected_categories = _write_entry_row(conn, entry, invalidate=invalidate, max_categories=max_categories)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    invalidate(affected_categories)
    # A hand-written identity note is the most deliberate statement of
    # identity there is.  Skipping fact derivation here left it invisible
    # to the startup prompt, which reads facts — the note only surfaced
    # later, if a query happened to route to free-form memory.
    for fact in facts._fact_assertions_from_entry(conn, entry):
        facts.add_fact_assertion(conn, fact)
    return entry


def merge_categories(
    conn,
    cat_a: str,
    cat_b: str,
    merged_name: str,
    *,
    invalidate,
    max_categories,
) -> None:
    """Merge cat_a and cat_b into merged_name, delete originals."""
    cat_a = normalize.normalize_category_name(cat_a)
    cat_b = normalize.normalize_category_name(cat_b)
    merged_name = normalize.normalize_category_name(merged_name)
    row_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM memory_items WHERE category IN (?, ?)",
            (cat_a, cat_b),
        ).fetchall()
    ]
    conn.execute(
        "UPDATE memory_items SET category = ?, updated_at = ? WHERE category IN (?, ?)",
        (merged_name, _now(), cat_a, cat_b),
    )
    for entry_id in row_ids:
        fts._sync_fts_row(conn, entry_id)
    invalidate({cat_a, cat_b, merged_name})

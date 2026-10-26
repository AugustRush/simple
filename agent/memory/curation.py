"""Curating memory by hand: the two writes a person makes to their own store.

Why this exists
---------------
Every other writer in this package is *derived*.  Consolidation decides what a
conversation taught, the fact ledger decides what an entry implies, retention
decides what has aged out.  None of them can answer "this is wrong, fix it" or
"this was never true" -- the one input they cannot synthesize is the person who
owns the memory.  The memory page is that input, and it needs exactly two
operations the store did not have: replace an entry's text, and make an entry
stop existing.

What is deliberately *not* here
-------------------------------
**No insert.**  ``add_entry`` already exists, already normalizes the category
and the entity, and already has callers.  A second insert path would be a second
answer to "what does writing a memory do"; the page's add button goes through
the same door the agent's own memory tools use.

**No fact writes.**  A fact is an *inference* from assertions.  Editing one by
hand would contradict the ledger that produced it, and the next consolidation
run would simply overwrite the edit.  To reject a belief the page calls
``retract_facts``: the assertions are archived, the resolved row is dropped, and
the next evidence resolves the key from scratch.  That is the honest way to say
"I don't believe this", and it is a different verb from "delete this note".

Why the pinned flag is a column and not a status
------------------------------------------------
``status`` already means something precise (``active`` / ``archived`` /
``superseded`` / ``conflicted``) and every read path filters on it.  A fifth
value would have to be taught to each of those filters, and the first one that
forgot would drop a pinned memory out of retrieval -- which is the exact failure
pinning exists to prevent.  A separate boolean is read by exactly two places
(retention, and the page that shows it) and changes no existing filter.

The trade-off is that a pin does not make an entry *more* retrievable: it only
stops retention from decaying and archiving it.  Ranking still sorts by
importance, so a pinned episode keeps the score it had.  Anything else would be
a ranking change disguised as a UI toggle.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from agent import shared

from . import fts, normalize
from . import rows as _row_mappers
from ._helpers import _now

__all__ = ["update_entry", "delete_entry"]


def _optional_row(conn: sqlite3.Connection, entry_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM memory_items WHERE id = ? LIMIT 1",
        (entry_id,),
    ).fetchone()


def update_entry(
    conn: sqlite3.Connection,
    entry_id: str,
    *,
    content: Optional[str] = None,
    importance: Optional[float] = None,
    category: Optional[str] = None,
    entity: Optional[str] = None,
    pinned: Optional[bool] = None,
    confidence: Optional[float] = None,
    max_categories: int = shared.MAX_CATEGORIES,
) -> Optional[Any]:
    """Apply the fields that were given; leave the rest alone.

    ``None`` means "the caller did not mention it", not "set it to empty" -- the
    page sends only what changed, so a field it never showed (``scope``, for
    instance) cannot be silently flattened by an edit of the text.

    A category change re-normalizes the entity against the *new* category,
    because an entity is only meaningful relative to its category (``user``
    under ``identity`` is a different thing from ``user`` under ``concepts``).
    """
    clean_id = str(entry_id or "").strip()
    if not clean_id:
        return None
    row = _optional_row(conn, clean_id)
    if row is None:
        return None
    current = _row_mappers._row_to_entry(row)

    updates: dict[str, Any] = {}
    if content is not None:
        cleaned = str(content).strip()
        if not cleaned:
            raise ValueError("记忆内容不能为空")
        updates["content"] = cleaned
    if importance is not None:
        updates["importance"] = max(0.0, min(1.0, float(importance)))
    if confidence is not None:
        updates["confidence"] = max(0.0, min(1.0, float(confidence)))
    if pinned is not None:
        updates["pinned"] = 1 if pinned else 0

    final_category = current.category
    if category is not None:
        from .entry_writes import _coerce_category_for_storage

        final_category = _coerce_category_for_storage(
            conn, str(category), max_categories=max_categories
        )
        updates["category"] = final_category

    if entity is not None:
        updates["entity"] = normalize._normalize_entity(str(entity), final_category)
    elif category is not None:
        # The stored entity was normalized against the old category.  An
        # *explicit* entity is the operator's word and survives the move, but a
        # default one ("general", or "user" under identity) is a fact about the
        # old category and has to be re-derived -- otherwise an entry moved to
        # ``identity`` keeps claiming ``general`` and the entity index stops
        # matching the thing it indexes.
        default_for_old = normalize._normalize_entity("", current.category)
        carried = "" if current.entity == default_for_old else current.entity
        updates["entity"] = normalize._normalize_entity(carried, final_category)

    if not updates:
        return current

    updates["updated_at"] = _now()
    assignments = ", ".join(f"{column} = ?" for column in updates)
    conn.execute(
        f"UPDATE memory_items SET {assignments} WHERE id = ?",
        (*updates.values(), clean_id),
    )
    fts._sync_fts_row(conn, clean_id)
    fresh = _optional_row(conn, clean_id)
    return _row_mappers._row_to_entry(fresh) if fresh is not None else None


def delete_entry(conn: sqlite3.Connection, entry_id: str) -> Optional[Any]:
    """Remove one entry, returning the row that was removed.

    Returning the entry rather than ``True`` is what lets the caller invalidate
    the category caches: after the delete the row is gone, so the category it
    belonged to cannot be read back from it.
    """
    clean_id = str(entry_id or "").strip()
    if not clean_id:
        return None
    row = _optional_row(conn, clean_id)
    if row is None:
        return None
    removed = _row_mappers._row_to_entry(row)
    conn.execute("DELETE FROM memory_items WHERE id = ?", (clean_id,))
    fts._delete_fts_rows(conn, [clean_id])
    return removed

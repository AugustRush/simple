"""Writing and scoring fact assertions -- the store's opinion ledger.

Extracted from ``LTMStore`` (see the remaining-big-files survey): four functions, each
`self`-free, none of which needs the store -- only the connection, the model types and the
helpers that define what a fact is.

Imported back into ``agent/memory/store.py`` so its call sites keep their sequence.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Optional

from agent.memory.models import FactAssertion
from . import rows as _row_mappers, normalize
from .models import ResolvedFact, LTMEntry
from ._helpers import (
    _FACT_SOURCE_PRECEDENCE,
    _IDENTITY_SUBJECTS,
    _RECENCY_GOVERNED_FACTS,
    _RETIRED_INFERENCE_SOURCE_KINDS,
    _dump_fact_value,
    _fact_value_type,
    _normalize_fact_part,
    _now,
    _fact_key,
    _ASSISTANT_IDENTITY_ENTITIES,
    _identity_note_value,
    _new_id,
)

__all__ = [
    "_insert_fact_assertion",
    "_fact_assertion_core_score",
    "_retract_inferred_identity_facts",
    "_turn_keys",
    "read_fact_assertions",
    "read_resolved_facts",
    "has_conflicted_fact",
    "retract_facts",
    "_read_facts",
    "_fact_assertion_total_score",
    "_fact_assertions_from_entry",
]


def _retract_inferred_identity_facts(
    conn: sqlite3.Connection
) -> set[tuple[str, str]]:
    subject_slots = ",".join("?" * len(_IDENTITY_SUBJECTS))
    source_slots = ",".join("?" * len(_RETIRED_INFERENCE_SOURCE_KINDS))
    params = tuple(sorted(_IDENTITY_SUBJECTS)) + _RETIRED_INFERENCE_SOURCE_KINDS
    rows = conn.execute(
        f"""
        SELECT DISTINCT subject, predicate FROM fact_assertions
        WHERE subject IN ({subject_slots})
          AND source_kind IN ({source_slots})
          AND status != 'archived'
        """,
        params,
    ).fetchall()
    if not rows:
        return set()
    conn.execute(
        f"""
        UPDATE fact_assertions SET status = 'archived', updated_at = ?
        WHERE subject IN ({subject_slots})
          AND source_kind IN ({source_slots})
          AND status != 'archived'
        """,
        (_now(),) + params,
    )
    return {(str(row["subject"]), str(row["predicate"])) for row in rows}


def _turn_keys(rows: Any) -> list[str]:
    """The distinct turns a run of positions belongs to, newest first.

    A position with no turn of its own -- an older conversation row written
    before ids were threaded through, a standalone command result -- is its
    own unit, which keeps such a row from being silently merged into the
    neighbouring turn's budget.
    """
    keys: list[str] = []
    for row in rows:
        key = str(row["turn_key"] or f"pos:{int(row['seq'])}")
        if key not in keys:
            keys.append(key)
    return keys


def _fact_assertion_core_score(
    assertion: FactAssertion,
) -> tuple[str, int, float, str]:
    """Rank competing assertions of the same fact; highest wins.

    For most facts a trusted source outranks a fresher one, so precedence
    leads.  Identity is different: it is a setting the user changes, and a
    setting is last-write-wins.  Letting precedence lead there meant an
    early statement pinned the identity permanently — a later correction
    arriving through any weaker channel could never displace it.

    ``created_at`` still trails the tuple for the non-identity case, so two
    assertions written in the same instant tie on every component and the
    caller declares a conflict rather than picking arbitrarily.
    """
    precedence = _FACT_SOURCE_PRECEDENCE.get(str(assertion.source_kind or "").strip().lower(), 9)
    created_at = assertion.created_at or ""
    key = (
        _normalize_fact_part(assertion.subject),
        _normalize_fact_part(assertion.predicate),
    )
    recency = created_at if key in _RECENCY_GOVERNED_FACTS else ""
    return (recency, -precedence, float(assertion.confidence or 0.0), created_at)


def _insert_fact_assertion(
    conn: sqlite3.Connection, assertion: FactAssertion
) -> None:
    """Write one assertion row. Does not re-resolve — callers decide when."""
    conn.execute(
        """
        INSERT OR IGNORE INTO fact_assertions (
            id, subject, predicate, value_json, value_type, scope,
            source_kind, source_id, source_session, channel, confidence,
            status, valid_from, valid_to, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            assertion.id,
            _normalize_fact_part(assertion.subject),
            _normalize_fact_part(assertion.predicate),
            _dump_fact_value(assertion.value),
            assertion.value_type or _fact_value_type(assertion.value),
            _normalize_fact_part(assertion.scope, "global"),
            _normalize_fact_part(assertion.source_kind, "manual_write"),
            assertion.source_id,
            assertion.source_session,
            assertion.channel,
            assertion.confidence,
            _normalize_fact_part(assertion.status, "active"),
            assertion.valid_from,
            assertion.valid_to,
            assertion.created_at or _now(),
            assertion.updated_at or _now(),
        ),
    )


def _read_facts(
    conn,
    table: str,
    order_by: str,
    to_row: Callable[[sqlite3.Row], Any],
    *,
    subject: Optional[str] = None,
    predicate: Optional[str] = None,
    scope: Optional[str] = None,
) -> list[Any]:
    """Filter one fact table by the three optional keys and read it back.

        Both fact tables carry the same ``subject``/``predicate``/``scope``
        triple and normalize it the same way, so the query lives here once and
        each caller supplies only what actually differs.  ``table`` and
        ``order_by`` are literals from this module, never caller input.
        """
    sql = f"SELECT * FROM {table} WHERE 1 = 1"
    params: list[Any] = []
    if subject is not None:
        sql += " AND subject = ?"
        params.append(_normalize_fact_part(subject))
    if predicate is not None:
        sql += " AND predicate = ?"
        params.append(_normalize_fact_part(predicate))
    if scope is not None:
        sql += " AND scope = ?"
        params.append(_normalize_fact_part(scope, "global"))
    sql += f" ORDER BY {order_by}"
    rows = conn.execute(sql, params).fetchall()
    return [to_row(row) for row in rows]


def read_fact_assertions(
    conn,
    *,
    subject: Optional[str] = None,
    predicate: Optional[str] = None,
    scope: Optional[str] = None,
) -> list[FactAssertion]:
    return _read_facts(
        conn, "fact_assertions",
        "created_at ASC, id ASC",
        _row_mappers._row_to_fact_assertion,
        subject=subject,
        predicate=predicate,
        scope=scope,
    )


def read_resolved_facts(
    conn,
    *,
    subject: Optional[str] = None,
    predicate: Optional[str] = None,
    scope: Optional[str] = None,
) -> list[ResolvedFact]:
    return _read_facts(
        conn, "resolved_facts",
        "subject ASC, predicate ASC, scope ASC",
        _row_mappers._row_to_resolved_fact,
        subject=subject,
        predicate=predicate,
        scope=scope,
    )


def has_conflicted_fact(
    conn,
    subject: str,
    predicate: str,
    scope: str = "global",
) -> bool:
    row = conn.execute(
        """
                SELECT 1
                FROM fact_assertions
                WHERE subject = ? AND predicate = ? AND scope = ?
                  AND status = 'conflicted'
                LIMIT 1
                """,
        (
            _normalize_fact_part(subject),
            _normalize_fact_part(predicate),
            _normalize_fact_part(scope, "global"),
        ),
    ).fetchone()
    return row is not None


def retract_facts(
    conn,
    subject: str,
    predicate: str,
    scope: str = "global",
) -> int:
    """Archive every assertion behind one fact and drop the resolved row."""
    cursor = conn.execute(
        """
                UPDATE fact_assertions SET status = 'archived', updated_at = ?
                WHERE subject = ? AND predicate = ? AND scope = ?
                  AND status != 'archived'
                """,
        (
            _now(),
            _normalize_fact_part(subject),
            _normalize_fact_part(predicate),
            _normalize_fact_part(scope, "global"),
        ),
    )
    archived = cursor.rowcount or 0
    conn.execute(
        "DELETE FROM resolved_facts WHERE fact_key = ?",
        (_fact_key(subject, predicate, scope),),
    )
    return archived


def _fact_assertion_total_score(
    assertion: FactAssertion,
) -> tuple[str, int, float, str, str]:
    return (*_fact_assertion_core_score(assertion), assertion.id)


def _fact_assertions_from_entry(
    conn,
    entry: LTMEntry,
) -> list[FactAssertion]:
    """Derive the identity *prose* an identity entry states, and only that.

        An entry is free text — a paragraph the user or consolidation wrote.
        Reading a name out of it means deciding what a sentence means, which is
        the model's job, not this module's; the extractor that used to try
        renamed the agent "什么" the first time somebody asked it its name.  So
        an entry contributes ``identity_note`` verbatim and nothing else.
        ``name`` and ``role`` come only from deliberate settings: config
        bootstrap and :meth:`set_identity`.
        """
    facts: list[FactAssertion] = []
    category = normalize.normalize_category_name(entry.category)
    entity = normalize._normalize_entity(entry.entity, category)
    # The assistant's identity is filed under several entities depending on
    # who wrote it: consolidation uses "assistant", a deliberate memory
    # write uses whatever name the caller passed.  Missing the latter meant
    # every hand-written identity update produced no fact at all, so the
    # first machine-extracted one stayed authoritative forever.
    if category != "identity" or entity not in _ASSISTANT_IDENTITY_ENTITIES:
        return facts
    note = _identity_note_value(entry.content)
    if not note:
        return facts
    source_kind = (
        "manual_write"
        if str(entry.source_session or "").strip() == "manual_memory_write"
        else "consolidation_extract"
    )
    facts.append(
        FactAssertion(
            id=_new_id(),
            subject="assistant",
            predicate="identity_note",
            value=note,
            source_kind=source_kind,
            source_id=entry.id,
            source_session=entry.source_session,
            confidence=float(entry.confidence or 1.0),
            created_at=entry.created_at or _now(),
            updated_at=entry.updated_at or _now(),
        )
    )
    return facts

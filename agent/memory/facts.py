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
    "resolve_fact",
    "add_fact_assertion",
    "set_identity",
    "_derive_missing_identity_facts",
    "_repair_assistant_identity_facts",
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
    """Resolved beliefs, each carrying the evidence row that produced it.

    The join is what makes "where did this come from" answerable.  Without it a
    belief is an assertion with no author, and the memory page cannot tell a
    sentence the operator typed from one the model inferred from a passing
    remark -- which are exactly the two things a person needs to distinguish
    before deciding whether to keep it.

    ``LEFT JOIN`` rather than ``JOIN``: a winning assertion can be gone (a
    database written before this join existed, a row archived while the
    resolved row survived), and a fact whose evidence is missing is still a
    fact this store believes.  Dropping it from the list would make the page
    quietly incomplete rather than visibly uncertain; the provenance fields
    come back empty and the page says so.
    """
    sql = """
        SELECT resolved_facts.*,
               fact_assertions.source_kind    AS evidence_source_kind,
               fact_assertions.source_session AS evidence_source_session,
               fact_assertions.channel        AS evidence_channel,
               fact_assertions.created_at     AS evidence_created_at
        FROM resolved_facts
        LEFT JOIN fact_assertions
               ON fact_assertions.id = resolved_facts.winning_assertion_id
        WHERE 1 = 1
    """
    params: list[Any] = []
    if subject is not None:
        sql += " AND resolved_facts.subject = ?"
        params.append(_normalize_fact_part(subject))
    if predicate is not None:
        sql += " AND resolved_facts.predicate = ?"
        params.append(_normalize_fact_part(predicate))
    if scope is not None:
        sql += " AND resolved_facts.scope = ?"
        params.append(_normalize_fact_part(scope, "global"))
    sql += " ORDER BY resolved_facts.subject ASC, resolved_facts.predicate ASC, resolved_facts.scope ASC"
    return [_row_mappers._row_to_resolved_fact(row) for row in conn.execute(sql, params).fetchall()]


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


def resolve_fact(
    conn,
    subject: str,
    predicate: str,
    scope: str = "global",
) -> Optional[ResolvedFact]:
    normalized_subject = _normalize_fact_part(subject)
    normalized_predicate = _normalize_fact_part(predicate)
    normalized_scope = _normalize_fact_part(scope, "global")
    key = _fact_key(normalized_subject, normalized_predicate, normalized_scope)

    rows = conn.execute(
        """
                SELECT *
                FROM fact_assertions
                WHERE subject = ? AND predicate = ? AND scope = ?
                  AND status != 'archived'
                ORDER BY created_at ASC, id ASC
                """,
        (normalized_subject, normalized_predicate, normalized_scope),
    ).fetchall()
    assertions = [_row_mappers._row_to_fact_assertion(row) for row in rows]
    if not assertions:
        conn.execute("DELETE FROM resolved_facts WHERE fact_key = ?", (key,))
        return None

    active = [assertion for assertion in assertions if not assertion.valid_to]
    if not active:
        conn.execute("DELETE FROM resolved_facts WHERE fact_key = ?", (key,))
        return None

    best_core_score = max(
        _fact_assertion_core_score(assertion) for assertion in active
    )
    top_assertions = [
        assertion
        for assertion in active
        if _fact_assertion_core_score(assertion) == best_core_score
    ]
    top_values = {
        _dump_fact_value(assertion.value) for assertion in top_assertions
    }

    if len(top_values) > 1:
        now = _now()
        conn.execute(
            """
                    UPDATE fact_assertions
                    SET status = 'conflicted',
                        updated_at = ?
                    WHERE subject = ? AND predicate = ? AND scope = ?
                      AND status != 'archived'
                    """,
            (now, normalized_subject, normalized_predicate, normalized_scope),
        )
        conn.execute("DELETE FROM resolved_facts WHERE fact_key = ?", (key,))
        return None

    winning_value_json = next(iter(top_values))
    winner = max(
        [
            assertion
            for assertion in active
            if _dump_fact_value(assertion.value) == winning_value_json
        ],
        key=_fact_assertion_total_score,
    )
    now = _now()
    for assertion in assertions:
        status = "active" if assertion.id == winner.id else "superseded"
        conn.execute(
            "UPDATE fact_assertions SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, assertion.id),
        )

    resolved = ResolvedFact(
        fact_key=key,
        subject=normalized_subject,
        predicate=normalized_predicate,
        value=winner.value,
        value_type=winner.value_type or _fact_value_type(winner.value),
        scope=normalized_scope,
        winning_assertion_id=winner.id,
        resolution_reason="resolved",
        confidence=float(winner.confidence or 0.0),
        resolved_at=now,
        updated_at=now,
    )
    conn.execute(
        """
                INSERT OR REPLACE INTO resolved_facts (
                    fact_key, subject, predicate, value_json, value_type, scope,
                    winning_assertion_id, resolution_reason, confidence,
                    resolved_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
        (
            resolved.fact_key,
            resolved.subject,
            resolved.predicate,
            _dump_fact_value(resolved.value),
            resolved.value_type,
            resolved.scope,
            resolved.winning_assertion_id,
            resolved.resolution_reason,
            resolved.confidence,
            resolved.resolved_at,
            resolved.updated_at,
        ),
    )
    return resolved


def _repair_assistant_identity_facts(conn) -> None:
    """Reconcile stored identity facts with the writers that exist today.

        Two kinds of damage accumulate in an existing store and neither heals
        on its own, because a fact is only ever written — never revisited.

        The pattern-matching extractor that used to read names out of chat is
        gone.  Everything it asserted was a guess made by code that no longer
        exists, and some of those guesses ("你叫什么" → name "什么") were
        recorded at the highest authority the store has, so nothing later could
        dislodge them.  Retract the lot by provenance rather than by inspecting
        values: which rows are unsound is a question about where they came
        from, and answering it by re-judging the text would just reinstate the
        guessing this removed.

        An identity note written by hand produced no fact at all, so the last
        machine-extracted note stayed current long after the user replaced it.
        Derive the missing facts now, keyed on the entry they came from so a
        second run adds nothing.

        Best-effort by construction: a store that cannot be repaired is still a
        store that works, and refusing to start over stale identity would be a
        far worse failure than carrying it.
        """
    try:
        retracted = _retract_inferred_identity_facts(conn)
        derived = _derive_missing_identity_facts(conn)
        for subject, predicate in retracted | derived:
            resolve_fact(conn, subject, predicate)
    except sqlite3.Error:
        return


def _derive_missing_identity_facts(
    conn: sqlite3.Connection,
) -> set[tuple[str, str]]:
    entity_slots = ",".join("?" * len(_ASSISTANT_IDENTITY_ENTITIES))
    rows = conn.execute(
        f"""
            SELECT * FROM memory_items
            WHERE category = 'identity' AND entity IN ({entity_slots})
              AND status NOT IN ('archived', 'superseded')
            """,
        tuple(sorted(_ASSISTANT_IDENTITY_ENTITIES)),
    ).fetchall()
    if not rows:
        return set()
    known = {
        (str(row["source_id"]), str(row["predicate"]))
        for row in conn.execute(
            "SELECT source_id, predicate FROM fact_assertions WHERE subject = 'assistant'"
        ).fetchall()
    }
    touched: set[tuple[str, str]] = set()
    for row in rows:
        entry = _row_mappers._row_to_entry(row)
        for fact in _fact_assertions_from_entry(conn, entry):
            if (str(fact.source_id), fact.predicate) in known:
                continue
            _insert_fact_assertion(conn, fact)
            touched.add((fact.subject, fact.predicate))
    return touched


def add_fact_assertion(
    conn,
    assertion: FactAssertion,
) -> FactAssertion:
    subject = _normalize_fact_part(assertion.subject)
    predicate = _normalize_fact_part(assertion.predicate)
    scope = _normalize_fact_part(assertion.scope, "global")
    if not subject or not predicate:
        raise ValueError("fact assertions require non-empty subject and predicate")

    normalized = FactAssertion(
        id=assertion.id or _new_id(),
        subject=subject,
        predicate=predicate,
        value=assertion.value,
        value_type=assertion.value_type or _fact_value_type(assertion.value),
        scope=scope,
        source_kind=_normalize_fact_part(assertion.source_kind, "manual_write"),
        source_id=str(assertion.source_id or "").strip(),
        source_session=str(assertion.source_session or "").strip(),
        channel=str(assertion.channel or "").strip(),
        confidence=float(assertion.confidence or 0.0),
        status=_normalize_fact_part(assertion.status, "active"),
        valid_from=str(assertion.valid_from or "").strip(),
        valid_to=str(assertion.valid_to or "").strip(),
        created_at=assertion.created_at or _now(),
        updated_at=assertion.updated_at or _now(),
    )

    _insert_fact_assertion(conn, normalized)
    resolve_fact(
        conn,
        normalized.subject,
        normalized.predicate,
        scope=normalized.scope,
    )
    return normalized


def set_identity(
    conn,
    *,
    subject: str = "assistant",
    name: Optional[str] = None,
    role: Optional[str] = None,
    persona: Optional[str] = None,
) -> dict[str, str]:
    """Record identity as a deliberate setting, last write wins.

        This is the only way ``name`` and ``role`` enter the store apart from
        config bootstrap.  Identity is something the user *sets*, not something
        the system infers from prose, so the caller states the value outright
        and it supersedes whatever was there — no pattern matching, no scoring
        an old value against a new one.

        Fields left as ``None`` are untouched; passing an empty string clears
        that field by superseding it with nothing.
        """
    normalized_subject = _normalize_fact_part(subject, "assistant")
    if normalized_subject not in _IDENTITY_SUBJECTS:
        raise ValueError(
            f"identity subject must be one of {sorted(_IDENTITY_SUBJECTS)}"
        )
    updates = {"name": name, "role": role, "identity_note": persona}
    applied: dict[str, str] = {}
    for predicate, raw in updates.items():
        if raw is None:
            continue
        value = str(raw).strip()
        if predicate == "identity_note":
            value = _identity_note_value(value)
        if not value:
            retract_facts(conn, normalized_subject, predicate)
            applied[predicate] = ""
            continue
        add_fact_assertion(
            conn,
            FactAssertion(
                id=_new_id(),
                subject=normalized_subject,
                predicate=predicate,
                value=value,
                source_kind="identity_directive",
                source_session="set_identity",
                confidence=1.0,
            )
        )
        applied[predicate] = value
    return applied

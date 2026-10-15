"""Writing and scoring fact assertions -- the store's opinion ledger.

Extracted from ``LTMStore`` (see the remaining-big-files survey): four functions, each
`self`-free, none of which needs the store -- only the connection, the model types and the
helpers that define what a fact is.

Imported back into ``agent/memory/store.py`` so its call sites keep their sequence.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from agent.memory.models import FactAssertion
from ._helpers import (
    _FACT_SOURCE_PRECEDENCE,
    _IDENTITY_SUBJECTS,
    _RECENCY_GOVERNED_FACTS,
    _RETIRED_INFERENCE_SOURCE_KINDS,
    _dump_fact_value,
    _fact_value_type,
    _normalize_fact_part,
    _now,
)

__all__ = [
    "_insert_fact_assertion",
    "_fact_assertion_core_score",
    "_retract_inferred_identity_facts",
    "_turn_keys",
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

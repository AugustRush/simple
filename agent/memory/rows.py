"""SQLite rows to the objects the contract hands out.

Six mappers from ``LTMStore`` (see the remaining-big-files survey): each takes a row and
returns the model object, and none of them touches the store -- they are pure translations,
which is what makes a second backend's job legible rather than archaeology.

Extracted from ``agent/memory/store.py`` and imported there, so the store's own call sites and
any reader of these names keep working unchanged.
"""

from __future__ import annotations

import json
import sqlite3

from ._helpers import _load_fact_value
from .models import (
    AgentRuntimeEvent,
    ConversationTurn,
    FactAssertion,
    LTMEntry,
    ResolvedFact,
    SessionWorkingState,
)

__all__ = [
    "_row_to_entry",
    "_row_to_conversation_turn",
    "_row_to_session_working_state",
    "_row_to_agent_runtime_event",
    "_row_to_fact_assertion",
    "_row_to_resolved_fact",
]


def _row_to_entry(row: sqlite3.Row) -> LTMEntry:
    return LTMEntry(
        id=row["id"],
        content=row["content"],
        importance=row["importance"],
        category=row["category"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        entity=row["entity"],
        memory_type=row["memory_type"],
        scope=row["scope"],
        status=row["status"],
        source_session=row["source_session"],
        confidence=row["confidence"],
    )


def _row_to_conversation_turn(row: sqlite3.Row) -> ConversationTurn:
    try:
        metadata = json.loads(row["metadata_json"] or "{}")
    except Exception:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return ConversationTurn(
        id=int(row["id"]),
        session_id=row["session_id"],
        role=row["role"],
        content=row["content"],
        channel=row["channel"],
        message_id=row["message_id"],
        reply_to_id=row["reply_to_id"],
        metadata=metadata,
        created_at=row["created_at"],
        seq=int(row["seq"] or 0),
    )


def _row_to_session_working_state(row: sqlite3.Row) -> SessionWorkingState:
    try:
        state = json.loads(row["state_json"] or "{}")
    except Exception:
        state = {}
    if not isinstance(state, dict):
        state = {}
    return SessionWorkingState(
        session_id=row["session_id"],
        state=state,
        updated_at=row["updated_at"],
    )


def _row_to_agent_runtime_event(row: sqlite3.Row) -> AgentRuntimeEvent:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return AgentRuntimeEvent(
        id=int(row["id"]),
        session_id=row["session_id"],
        turn_id=row["turn_id"],
        event_type=row["event_type"],
        payload=payload,
        created_at=row["created_at"],
        seq=int(row["seq"] or 0),
    )


def _row_to_fact_assertion(row: sqlite3.Row) -> FactAssertion:
    return FactAssertion(
        id=row["id"],
        subject=row["subject"],
        predicate=row["predicate"],
        value=_load_fact_value(row["value_json"]),
        value_type=row["value_type"],
        scope=row["scope"],
        source_kind=row["source_kind"],
        source_id=row["source_id"],
        source_session=row["source_session"],
        channel=row["channel"],
        confidence=float(row["confidence"] or 1.0),
        status=row["status"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_resolved_fact(row: sqlite3.Row) -> ResolvedFact:
    return ResolvedFact(
        fact_key=row["fact_key"],
        subject=row["subject"],
        predicate=row["predicate"],
        value=_load_fact_value(row["value_json"]),
        value_type=row["value_type"],
        scope=row["scope"],
        winning_assertion_id=row["winning_assertion_id"],
        resolution_reason=row["resolution_reason"],
        confidence=float(row["confidence"] or 1.0),
        resolved_at=row["resolved_at"],
        updated_at=row["updated_at"],
    )

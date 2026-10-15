"""The journal: conversation turns, session titles, working state, checkpoints, agent events.

Twelve methods moved out of ``LTMStore`` (see the remaining-big-files survey).  Each took the
connection and nothing else -- a ``self.X`` count could not tell them from delegate wrappers, so
they were re-checked against the class before moving, and all twelve were live bodies.

Two of them are read-modify-write with a caller-supplied timestamp (``save_provider_checkpoint``,
``append_agent_event``): the journal is append-only in spirit, and the only reason a row is ever
replaced is that a session's working state is a snapshot rather than a history.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from . import migrations
from . import rows as _row_mappers
from ._helpers import _now
from .models import AgentRuntimeEvent, SessionWorkingState

__all__ = [
    "message_has_answer",
    "conversation_turn_count",
    "delete_conversation_session",
    "set_session_title",
    "list_session_titles",
    "load_session_working_state",
    "save_session_working_state",
    "save_provider_checkpoint",
    "load_provider_checkpoint",
    "append_agent_event",
    "recent_agent_events",
    "recent_agent_events_for_turns",
]


def message_has_answer(
    conn,
    *,
    session_id: str,
    message_id: str,
) -> bool:
    """Whether this message already has an assistant answer on record.

        The question a re-delivery has to answer before it becomes work again.
        A message that has been answered is a resend, not a task: running it a
        second time pays for the whole tool loop twice for a reply the reader
        already has.
        """
    clean_session = str(session_id or "").strip()
    clean_message = str(message_id or "").strip()
    if not clean_session or not clean_message:
        return False
    row = conn.execute(
        """
                SELECT 1 FROM conversation_turns
                 WHERE session_id = ? AND role = 'assistant'
                   AND (reply_to_id = ? OR message_id = ?)
                 LIMIT 1
                """,
        (clean_session, clean_message, clean_message),
    ).fetchone()
    return row is not None


def conversation_turn_count(
    conn,
    *,
    session_id: Optional[str] = None,
) -> int:
    """How many journaled turns exist, so a capped read can say it was capped."""
    if session_id:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM conversation_turns WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM conversation_turns"
        ).fetchone()
    return int(row["total"] if row else 0)


def delete_conversation_session(
    conn,
    session_id: str,
) -> None:
    """Delete a conversation session's journaled turns and working state."""
    clean_session_id = str(session_id or "").strip()
    if not clean_session_id:
        return
    conn.execute(
        "DELETE FROM conversation_turns WHERE session_id = ?",
        (clean_session_id,),
    )
    conn.execute(
        "DELETE FROM session_working_state WHERE session_id = ?",
        (clean_session_id,),
    )
    conn.execute(
        "DELETE FROM provider_checkpoints WHERE session_id = ?",
        (clean_session_id,),
    )
    conn.execute(
        "DELETE FROM agent_events WHERE session_id = ?",
        (clean_session_id,),
    )
    conn.execute(
        "DELETE FROM usage_events WHERE session_id = ?",
        (clean_session_id,),
    )
    conn.execute(
        "DELETE FROM session_meta WHERE session_id = ?",
        (clean_session_id,),
    )


def set_session_title(
    conn,
    session_id: str,
    title: str,
) -> None:
    clean_session_id = str(session_id or "").strip()
    if not clean_session_id:
        return
    clean_title = str(title or "").strip()[:120]
    conn.execute(
        """
                INSERT INTO session_meta (session_id, title, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    title = excluded.title,
                    updated_at = excluded.updated_at
                """,
        (clean_session_id, clean_title, _now()),
    )


def list_session_titles(conn) -> dict[str, str]:
    rows = conn.execute(
        "SELECT session_id, title FROM session_meta WHERE title <> ''"
    ).fetchall()
    return {str(row["session_id"]): str(row["title"]) for row in rows}


def load_session_working_state(
    conn,
    session_id: str,
) -> Optional[SessionWorkingState]:
    clean_session_id = str(session_id or "").strip()
    if not clean_session_id:
        return None
    row = conn.execute(
        "SELECT * FROM session_working_state WHERE session_id = ?",
        (clean_session_id,),
    ).fetchone()
    return _row_mappers._row_to_session_working_state(row) if row else None


def save_session_working_state(
    conn,
    session_id: str,
    state: dict[str, Any],
    *,
    updated_at: Optional[str] = None,
) -> SessionWorkingState:
    clean_session_id = str(session_id or "").strip() or "default"
    clean_state = state if isinstance(state, dict) else {"value": state}
    ts = updated_at or _now()
    payload = json.dumps(clean_state, ensure_ascii=False, sort_keys=True)
    conn.execute(
        """
                INSERT INTO session_working_state (session_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
        (clean_session_id, payload, ts),
    )
    row = conn.execute(
        "SELECT * FROM session_working_state WHERE session_id = ?",
        (clean_session_id,),
    ).fetchone()
    return _row_mappers._row_to_session_working_state(row)


def save_provider_checkpoint(
    conn,
    session_id: str,
    messages: list[dict[str, Any]],
    *,
    summary: str = "",
    updated_at: Optional[str] = None,
) -> None:
    clean = str(session_id or "").strip() or "default"
    payload = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str)
    conn.execute(
        """
                INSERT INTO provider_checkpoints (
                    session_id, messages_json, summary, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    messages_json = excluded.messages_json,
                    summary = excluded.summary,
                    updated_at = excluded.updated_at
                """,
        (clean, payload, str(summary or ""), updated_at or _now()),
    )


def load_provider_checkpoint(
    conn,
    session_id: str,
) -> dict[str, Any] | None:
    clean = str(session_id or "").strip()
    if not clean:
        return None
    row = conn.execute(
        "SELECT * FROM provider_checkpoints WHERE session_id = ?",
        (clean,),
    ).fetchone()
    if row is None:
        return None
    try:
        messages = json.loads(str(row["messages_json"] or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        messages = []
    if not isinstance(messages, list):
        messages = []
    return {
        "session_id": clean,
        "messages": [item for item in messages if isinstance(item, dict)],
        "summary": str(row["summary"] or ""),
        "updated_at": str(row["updated_at"] or ""),
    }


def append_agent_event(
    conn,
    *,
    session_id: str,
    event_type: str,
    payload: Optional[dict[str, Any]] = None,
    turn_id: str = "",
    created_at: Optional[str] = None,
) -> AgentRuntimeEvent:
    clean_session_id = str(session_id or "").strip() or "default"
    clean_event_type = str(event_type or "").strip() or "event"
    clean_payload = payload if isinstance(payload, dict) else {}
    ts = created_at or _now()
    cur = conn.execute(
        """
                INSERT INTO agent_events (
                    session_id, turn_id, event_type, payload_json, created_at, seq
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
        (
            clean_session_id,
            str(turn_id or ""),
            clean_event_type,
            json.dumps(clean_payload, ensure_ascii=False, sort_keys=True),
            ts,
            # From the same counter the conversation rows draw on, so a
            # tool call and the reply it fed share one timeline.
            migrations._next_transcript_seq(conn, clean_session_id),
        ),
    )
    row = conn.execute(
        "SELECT * FROM agent_events WHERE id = ?",
        (cur.lastrowid,),
    ).fetchone()
    return _row_mappers._row_to_agent_runtime_event(row)


def recent_agent_events(
    conn,
    *,
    session_id: str,
    limit: int = 20,
) -> list[AgentRuntimeEvent]:
    clean_session_id = str(session_id or "").strip()
    if not clean_session_id:
        return []
    # Tool progress is much more verbose than conversation turns. Allow
    # the web history reader to fetch enough events to rebuild older traces
    # after a restart while retaining a hard upper bound.
    limit = max(1, min(int(limit), 2000))
    rows = conn.execute(
        """
                SELECT * FROM agent_events
                WHERE session_id = ?
                ORDER BY id DESC LIMIT ?
                """,
        (clean_session_id, limit),
    ).fetchall()
    return [_row_mappers._row_to_agent_runtime_event(row) for row in reversed(rows)]


def recent_agent_events_for_turns(
    conn,
    *,
    turn_ids: list[str] | tuple[str, ...] | set[str],
    limit: int = 2000,
) -> list[AgentRuntimeEvent]:
    """Read events for known turns, including legacy mis-scoped rows.

        Older Web runtimes persisted events through a factory-level staging
        buffer, so their ``session_id`` can differ from the conversation row
        while the globally unique ``turn_id`` remains correct. This narrow
        lookup lets history replay recover those rows without broadening the
        session query to unrelated events.
        """
    ids = [str(value or "").strip() for value in turn_ids]
    ids = list(dict.fromkeys(value for value in ids if value))
    if not ids:
        return []
    limit = max(1, min(int(limit), 2000))
    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"""
                SELECT * FROM agent_events
                WHERE turn_id IN ({placeholders})
                ORDER BY id DESC LIMIT ?
                """,
        (*ids, limit),
    ).fetchall()
    return [_row_mappers._row_to_agent_runtime_event(row) for row in reversed(rows)]

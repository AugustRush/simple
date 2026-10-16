""""""

from __future__ import annotations

from . import migrations
from ._helpers import _now
from .models import ConversationWriteResult
import json

from typing import Any, Optional, Sequence

__all__ = [
    "_json_object",
    "write_conversation_exchange",
]


def _json_object(raw: Any) -> dict[str, Any]:
    """The object a stored ``metadata_json`` holds, or an empty one."""
    try:
        value = json.loads(raw or "{}")
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def write_conversation_exchange(
    conn,
    *,
    session_id: str,
    user_content: str,
    assistant_content: str = "",
    channel: str = "",
    message_id: str = "",
    assistant_message_id: str = "",
    reply_to_id: str = "",
    metadata: Optional[dict[str, Any]] = None,
    interjections: Optional[Sequence[dict[str, Any]]] = None,
    created_at: Optional[str] = None,
) -> ConversationWriteResult:
    """Idempotently journal a user event and optional assistant completion.

        A non-empty message ID is an idempotency key for the user event.  The
        user row is committed before an LLM request can begin; a later call can
        attach the assistant row without duplicating the user event.

        ``interjections`` are the messages the user sent *during* the turn and
        the model read mid-flight.  They are written here, between the request
        that opened the turn and the answer it produced, because that is where
        they happened: the model answered them within this same turn, so a
        record without them claims the answer came from nowhere -- and the
        reader loses the words that asked for it.  Each row carries an
        ``interjection`` marker, which is what lets a client render it as
        in-turn speech rather than as the start of another turn.
        """
    payload = metadata or {}
    if not isinstance(payload, dict):
        payload = {"value": payload}
    clean_user_content = str(user_content or "").strip()
    has_attachments = bool(payload.get("attachments"))
    if not clean_user_content and not has_attachments:
        return ConversationWriteResult()
    clean_assistant_content = str(assistant_content or "").strip()
    clean_session_id = str(session_id or "").strip() or "default"
    clean_message_id = str(message_id or "").strip()
    clean_assistant_message_id = str(assistant_message_id or "").strip()
    metadata_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    timestamp = created_at or _now()

    user_created = False
    assistant_created = False
    first_assistant_for_user = False
    interjections_created: list[str] = []
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        existing_user = None
        if clean_message_id:
            existing_user = conn.execute(
                """
                    SELECT id FROM conversation_turns
                    WHERE session_id = ? AND role = 'user' AND message_id = ?
                    LIMIT 1
                    """,
                (clean_session_id, clean_message_id),
            ).fetchone()
        if existing_user is None:
            conn.execute(
                """
                    INSERT INTO conversation_turns (
                        session_id, role, content, channel, message_id, reply_to_id,
                        metadata_json, created_at, seq
                    ) VALUES (?, 'user', ?, ?, ?, ?, ?, ?, ?)
                    """,
                (
                    clean_session_id,
                    clean_user_content,
                    str(channel or ""),
                    clean_message_id,
                    str(reply_to_id or ""),
                    metadata_json,
                    timestamp,
                    migrations._next_transcript_seq(conn, clean_session_id),
                ),
            )
            user_created = True
        # After the request that opened the turn and before its answer:
        # row ids are the display order, and this is where the message
        # happened.  Also after, not before, so the turn's own request
        # stays the first row a reader sees for it.
        for entry in interjections or ():
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            entry_id = str(entry.get("message_id") or "").strip()
            if entry_id:
                already = conn.execute(
                    """
                        SELECT id FROM conversation_turns
                        WHERE session_id = ? AND role = 'user' AND message_id = ?
                        LIMIT 1
                        """,
                    (clean_session_id, entry_id),
                ).fetchone()
                if already is not None:
                    continue
            entry_metadata: dict[str, Any] = {
                "interjection": True,
                "urgency": str(entry.get("urgency") or "normal"),
            }
            if entry.get("arrived_at"):
                entry_metadata["arrived_at"] = entry["arrived_at"]
            conn.execute(
                """
                    INSERT INTO conversation_turns (
                        session_id, role, content, channel, message_id, reply_to_id,
                        metadata_json, created_at, seq
                    ) VALUES (?, 'user', ?, ?, ?, ?, ?, ?, ?)
                    """,
                (
                    clean_session_id,
                    text,
                    str(channel or ""),
                    entry_id,
                    clean_message_id,
                    json.dumps(entry_metadata, ensure_ascii=False, sort_keys=True),
                    timestamp,
                    migrations._next_transcript_seq(conn, clean_session_id),
                ),
            )
            interjections_created.append(text)
        if clean_assistant_content:
            existing_assistant = None
            if clean_assistant_message_id:
                existing_assistant = conn.execute(
                    """
                        SELECT id, metadata_json FROM conversation_turns
                        WHERE session_id = ? AND role = 'assistant'
                          AND message_id = ?
                        LIMIT 1
                        """,
                    (clean_session_id, clean_assistant_message_id),
                ).fetchone()
            elif clean_message_id:
                existing_assistant = conn.execute(
                    """
                        SELECT id, metadata_json FROM conversation_turns
                        WHERE session_id = ? AND role = 'assistant'
                          AND reply_to_id = ?
                        LIMIT 1
                        """,
                    (clean_session_id, clean_message_id),
                ).fetchone()
            if existing_assistant is not None:
                # Idempotency is per *execution*: a write this run already
                # made collapses into its own row, while another run's answer
                # is a row of its own.  Keying on the completion id alone made
                # a second execution's answer look like a duplicate of the
                # first and dropped it -- measurably, two delivered answers
                # were missing from the record while the rows held *older*
                # text.  Rows written before runs had ids keep the old
                # behaviour, so this only ever adds an answer.
                incoming_run = str(payload.get("run_id") or "")
                existing_run = str(
                    _json_object(existing_assistant["metadata_json"]).get("run_id") or ""
                )
                if incoming_run and existing_run and incoming_run != existing_run:
                    existing_assistant = None
            if existing_assistant is None:
                prior_assistant = None
                if clean_message_id:
                    prior_assistant = conn.execute(
                        """
                            SELECT id FROM conversation_turns
                            WHERE session_id = ? AND role = 'assistant'
                              AND reply_to_id = ?
                            LIMIT 1
                            """,
                        (clean_session_id, clean_message_id),
                    ).fetchone()
                conn.execute(
                    """
                        INSERT INTO conversation_turns (
                            session_id, role, content, channel, message_id, reply_to_id,
                            metadata_json, created_at, seq
                        ) VALUES (?, 'assistant', ?, ?, ?, ?, ?, ?, ?)
                        """,
                    (
                        clean_session_id,
                        clean_assistant_content,
                        str(channel or ""),
                        clean_assistant_message_id,
                        clean_message_id,
                        metadata_json,
                        timestamp,
                        migrations._next_transcript_seq(conn, clean_session_id),
                    ),
                )
                assistant_created = True
                first_assistant_for_user = prior_assistant is None
    return ConversationWriteResult(
        user_created=user_created,
        assistant_created=assistant_created,
        first_assistant_for_user=first_assistant_for_user,
        interjections_created=tuple(interjections_created),
    )

"""Reading the conversation journal: which sessions exist, and what was said in them.

Moved out of ``LTMStore`` (see the remaining-big-files survey).  Each function takes the
connection as its first argument rather than opening one; the store keeps the thread-local
connection and the transaction handling, and these keep the SQL.
"""

from __future__ import annotations

from typing import Any, Optional

from agent import shared

from . import rows as _row_mappers
from ._helpers import _lexical_terms
from .models import ConversationTurn

__all__ = ['list_session_ids', 'recent_conversation_turns', 'search_conversation_turns']


def recent_conversation_turns(conn,
    *,
    session_id: Optional[str] = None,
    channel: Optional[str] = None,
    exclude_message_id: str = "",
    limit: int = shared.RECENT_SESSION_TURNS,
) -> list[ConversationTurn]:
    # 100 was a hard ceiling that silently overrode the caller's own
    # window -- the web transcript asked for 500 and received 100, so a
    # long session lost its beginning with nothing to say so.  The
    # ceiling now only bounds a single read; callers that pass a small
    # limit (the memory paths pass 6 by default) are unaffected.
    limit = max(1, min(int(limit), 500))
    sql = "SELECT * FROM conversation_turns"
    params: list[Any] = []
    clauses: list[str] = []
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if channel:
        clauses.append("channel = ?")
        params.append(channel)
    clean_exclude_id = str(exclude_message_id or "").strip()
    if clean_exclude_id:
        clauses.append(
            "NOT ((role = 'user' AND message_id = ?) "
            "OR (role = 'assistant' AND reply_to_id = ?))"
        )
        params.extend((clean_exclude_id, clean_exclude_id))
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    # ``seq``, not ``id``: the transcript's order is the session-wide
    # timeline, and ``id`` is only monotonic within this table.
    sql += " ORDER BY seq DESC, id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_row_mappers._row_to_conversation_turn(row) for row in reversed(rows)]


def list_session_ids(conn,
    prefix: str = "",
    limit: int = 50,
) -> list[tuple[str, str, int]]:
    """Return distinct journaled sessions, most recently active first.

    Each item is ``(session_id, last_activity_iso, turn_count)``.  This
    enumerates the durable conversations recorded in the
    ``conversation_turns`` journal for the current agent home (for example
    the per-chat sessions a gateway multiplexes).
    """
    limit = max(1, min(int(limit), 500))
    clean_prefix = str(prefix or "").strip()
    if clean_prefix:
        rows = conn.execute(
            """
            SELECT session_id, MAX(created_at) AS last_activity,
                   COUNT(*) AS turns
            FROM conversation_turns
            WHERE session_id LIKE ?
            GROUP BY session_id
            ORDER BY last_activity DESC, session_id ASC
            LIMIT ?
            """,
            (clean_prefix + "%", limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT session_id, MAX(created_at) AS last_activity,
                   COUNT(*) AS turns
            FROM conversation_turns
            GROUP BY session_id
            ORDER BY last_activity DESC, session_id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        (
            str(row["session_id"]),
            str(row["last_activity"] or ""),
            int(row["turns"] or 0),
        )
        for row in rows
    ]


def search_conversation_turns(conn,
    query: str,
    *,
    session_id: str,
    exclude_message_id: str = "",
    limit: int = shared.RECENT_SESSION_TURNS,
) -> list[ConversationTurn]:
    terms = [term for term in _lexical_terms(query) if len(term) >= 2][:12]
    if not terms:
        return []
    clauses = " OR ".join("content LIKE ?" for _ in terms)
    clean_exclude_id = str(exclude_message_id or "").strip()
    exclusion_sql = ""
    exclusion_params: list[Any] = []
    if clean_exclude_id:
        exclusion_sql = (
            " AND NOT ((role = 'user' AND message_id = ?) "
            "OR (role = 'assistant' AND reply_to_id = ?))"
        )
        exclusion_params = [clean_exclude_id, clean_exclude_id]
    params: list[Any] = [
        session_id,
        *(f"%{term}%" for term in terms),
        *exclusion_params,
        limit,
    ]
    rows = conn.execute(
        f"""
        SELECT * FROM conversation_turns
        WHERE session_id = ? AND ({clauses}){exclusion_sql}
        ORDER BY id DESC LIMIT ?
        """,
        params,
    ).fetchall()
    rows_by_id = {int(row["id"]): row for row in rows}
    for row in list(rows):
        neighbor_id = int(row["id"]) + (1 if row["role"] == "user" else -1)
        neighbor = conn.execute(
            """
            SELECT * FROM conversation_turns
            WHERE id = ? AND session_id = ?
              AND (? = '' OR NOT (
                  (role = 'user' AND message_id = ?)
                  OR (role = 'assistant' AND reply_to_id = ?)
              ))
            LIMIT 1
            """,
            (
                neighbor_id,
                session_id,
                clean_exclude_id,
                clean_exclude_id,
                clean_exclude_id,
            ),
        ).fetchone()
        if neighbor is None:
            continue
        if row["role"] == "user" and neighbor["role"] != "assistant":
            continue
        if row["role"] == "assistant" and neighbor["role"] != "user":
            continue
        rows_by_id[int(neighbor["id"])] = neighbor
    ordered = sorted(rows_by_id.values(), key=lambda row: int(row["id"]))
    return [_row_mappers._row_to_conversation_turn(row) for row in ordered[-limit * 2 :]]

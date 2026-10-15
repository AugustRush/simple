"""Reading the conversation journal: which sessions exist, and what was said in them.

Moved out of ``LTMStore`` (see the remaining-big-files survey).  Each function takes the
connection as its first argument rather than opening one; the store keeps the thread-local
connection and the transaction handling, and these keep the SQL.
"""

from __future__ import annotations

import sqlite3

from typing import Any, Optional

from agent import shared

from agent.lexical import LATIN_TOKEN_RE
from .retrieval import CorpusStats
from . import rows as _row_mappers, facts
from ._helpers import _lexical_terms
from .models import ConversationTurn, AgentRuntimeEvent

__all__ = [
    "corpus_stats",
    "transcript_window",
    "_rows_at_seqs",'list_session_ids', 'recent_conversation_turns', 'search_conversation_turns']


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


_CJK_DF_SAMPLE_SIZE = 2000


def transcript_window(conn,
    *,
    session_id: str,
    limit: int = 200,
    before_seq: int = 0,
) -> tuple[list[ConversationTurn], list[AgentRuntimeEvent], bool]:
    """One window of the transcript: both tables, one order, whether more exists.

    Ordered by ``seq`` alone, because both tables draw their positions from
    the same per-session counter -- the reader is handed a timeline rather
    than two lists it has to reconcile, and a tool call keeps its place
    between the request that caused it and the reply it fed.

    ``limit`` bounds **turns**, not positions.  A position is not what the
    client consumes: a turn's tool calls take two positions each and are
    displayed as one collapsed trace, so a position budget can be spent
    inside a single turn -- the reader is handed that trace with no reply,
    and the conversation it wanted sits behind the cursor until somebody
    pages back through a hundred tool calls.  Measured: a session whose
    ``conversation_turns`` held five rows answered ``limit=200`` with 101
    tool rows and no assistant row at all, because one turn's events filled
    the window on their own.

    A turn is the unit writer and reader agree on: ``agent_events.turn_id``
    names it, ``conversation_turns`` links its rows through
    ``reply_to_id``/``message_id``, and a turn's positions are contiguous.
    So the window walks positions newest-first and cuts at the boundary of
    the next turn, which is what keeps a turn's tools and its reply in the
    same window.  ``before_seq`` pages backwards through it.
    """
    clean_session_id = str(session_id or "").strip()
    if not clean_session_id:
        return [], [], False
    turns_limit = max(1, min(int(limit), 500))
    window = before_seq if before_seq > 0 else (1 << 62)
    # Read positions newest-first in chunks until the budget of *turns* is
    # full: how many positions one turn owns is the turn's business, so the
    # only way to count turns is to walk them.
    withheld = False
    collected: list[Any] = []
    batch = max(turns_limit * 2, 200)
    while True:
        chunk = conn.execute(
            """
            SELECT seq, 'turn' AS source,
                   COALESCE(NULLIF(reply_to_id, ''), message_id) AS turn_key
              FROM conversation_turns
             WHERE session_id = ? AND seq < ?
            UNION ALL
            SELECT seq, 'event' AS source, turn_id AS turn_key
              FROM agent_events
             WHERE session_id = ? AND seq < ?
               AND event_type IN (
                   'tool_started', 'tool_progress', 'tool_completed',
                   'tool_failed', 'tool_blocked', 'attachment',
                   'command_result'
               )
             ORDER BY seq DESC
             LIMIT ?
            """,
            (clean_session_id, window, clean_session_id, window, batch),
        ).fetchall()
        collected.extend(chunk)
        if len(chunk) < batch:
            break
        if len(facts._turn_keys(collected)) > turns_limit:
            withheld = True
            break
        window = int(chunk[-1]["seq"])

    positions: list[Any] = []
    keys: list[str] = []
    for row in collected:  # newest first
        key = str(row["turn_key"] or f"pos:{int(row['seq'])}")
        if key not in keys:
            if len(keys) >= turns_limit:
                withheld = True
                break
            keys.append(key)
        positions.append(row)
    has_more = withheld and bool(positions)
    turn_seqs = [
        int(row["seq"]) for row in positions if str(row["source"]) == "turn"
    ]
    event_seqs = [
        int(row["seq"]) for row in positions if str(row["source"]) == "event"
    ]

    # Resolved through each row's own sequence rather than by re-running a
    # range query: the window is a set of positions, and a second range
    # query could pick up rows written since this one was taken.
    return (
        [
            _row_mappers._row_to_conversation_turn(row)
            for row in _rows_at_seqs(
                conn, clean_session_id, "conversation_turns", turn_seqs
            )
        ],
        [
            _row_mappers._row_to_agent_runtime_event(row)
            for row in _rows_at_seqs(
                conn, clean_session_id, "agent_events", event_seqs
            )
        ],
        has_more,
    )


def _rows_at_seqs(conn, session_id: str, table: str, seqs: list[int]
) -> list[sqlite3.Row]:
    """Rows of one session's ``table`` at these positions, in order.

    Scoped to the session as well as the positions: a sequence is only
    unique within a session, so resolving rows by position alone returns
    another conversation's rows wherever the two sets of numbers happen to
    line up.
    """
    if not seqs:
        return []
    placeholders = ", ".join("?" for _ in seqs)
    return conn.execute(
        f"SELECT * FROM {table} WHERE session_id = ?"
        f" AND seq IN ({placeholders}) ORDER BY seq",
        (session_id, *seqs),
    ).fetchall()


def corpus_stats(conn,
    terms: list[str],
    scopes: Optional[list[str]] = None,
) -> "CorpusStats":
    """Corpus-wide document frequencies for *terms*, plus the corpus size.

    BM25's IDF is only meaningful relative to a corpus.  Computing it over
    an already-retrieved candidate list inverts the signal: those
    candidates were selected *because* they match the query, so every
    query term has df ≈ N there and scores near zero IDF — the retriever
    ends up ignoring exactly the words the user asked about.

    Counting is **bounded**, not exact.  IDF is a logarithm, so all of its
    discriminative power sits in the low-df regime: "3 documents vs 300"
    moves the score a lot, "3,000 vs 8,000" moves it by a hair.

    The two term kinds get different treatment because their matching
    costs differ by orders of magnitude:

    - Latin tokens match through the FTS index, so an exact count is
      cheap; it is merely capped so a ubiquitous term cannot cost a full
      scan.
    - CJK n-grams match with ``LIKE '%x%'``, which no index can serve —
      an exact count is a full table scan *per term*, and one Chinese
      query expands to ~25 n-grams.  Estimating df from a single bounded
      sample of documents gives the ratio df/N (all IDF actually needs)
      at a cost independent of store size, with one query for every term
      instead of one per term.
    """
    if not terms:
        return CorpusStats(total_documents=0, document_frequencies={})

    latin_terms = [term for term in terms if LATIN_TOKEN_RE.fullmatch(term)]
    # Mirror search_entries' own CJK cap so df is measured over the same
    # terms that actually drive retrieval.
    cjk_terms = [term for term in terms if term not in latin_terms][:12]
    scope_clause = ""
    scope_params: list[Any] = []
    if scopes:
        scope_clause = f" AND scope IN ({','.join('?' for _ in scopes)})"
        scope_params = [str(scope) for scope in scopes]

    frequencies: dict[str, int] = {}
    total = int(
        conn.execute(
            "SELECT count(*) FROM memory_items "
            "WHERE status NOT IN ('archived', 'superseded')" + scope_clause,
            scope_params,
        ).fetchone()[0]
        or 0
    )
    # A term in more than ~10% of the store carries no useful IDF.
    cap = max(64, total // 10)

    for term in latin_terms:
        escaped = term.replace('"', '""')
        row = conn.execute(
            f"""
            SELECT count(*) FROM (
                SELECT 1
                FROM memory_items_fts
                JOIN memory_items AS m ON m.id = memory_items_fts.memory_id
                WHERE memory_items_fts MATCH ?
                  AND m.status NOT IN ('archived', 'superseded')
                {scope_clause.replace(" AND scope", " AND m.scope")}
                LIMIT {cap}
            )
            """,
            [f'"{escaped}"*', *scope_params],
        ).fetchone()
        found = int(row[0] or 0) if row else 0
        # Hit the cap → treat as ubiquitous rather than guessing.
        frequencies[term] = total if found >= cap else found

    if cjk_terms and total:
        sample_size = min(total, _CJK_DF_SAMPLE_SIZE)
        # `ORDER BY id` is a stable pseudo-random sample, not a
        # chronological one: ids are `uuid4().hex[:12]`, so ordering
        # by them is uncorrelated with content or age.  A sequential
        # id scheme would bias this toward the oldest memories.
        rows = conn.execute(
            """
            SELECT content, entity, category
            FROM memory_items
            WHERE status NOT IN ('archived', 'superseded')
            """
            + scope_clause
            + " ORDER BY id LIMIT ?",
            [*scope_params, sample_size],
        ).fetchall()
        haystacks = [
            f"{row['content']}\n{row['entity']}\n{row['category']}"
            for row in rows
        ]
        observed = len(haystacks) or 1
        for term in cjk_terms:
            hits = sum(1 for text in haystacks if term in text)
            # Scale the sample ratio back up to the whole corpus.
            frequencies[term] = min(total, round(total * hits / observed))
    else:
        for term in cjk_terms:
            frequencies[term] = 0

    return CorpusStats(total_documents=total, document_frequencies=frequencies)

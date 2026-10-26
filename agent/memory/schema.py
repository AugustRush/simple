"""Creating and repairing the store's schema: tables, FTS index, migrations.

Moved out of `LTMStore` (see the remaining-big-files survey).  Both take the connection as their
first argument: schema setup is a property of a database file, not of the object that happens
to hold one."""

from __future__ import annotations

from . import migrations

__all__ = [
    "_ensure_schema",
    "_ensure_fts_index",]


def _ensure_schema(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_items (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            importance REAL NOT NULL,
            category TEXT NOT NULL,
            entity TEXT NOT NULL DEFAULT '',
            memory_type TEXT NOT NULL DEFAULT 'fact',
            scope TEXT NOT NULL DEFAULT 'global',
            status TEXT NOT NULL DEFAULT 'active',
            source_session TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 1.0,
            pinned INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memory_category_status
            ON memory_items(category, status);
        CREATE INDEX IF NOT EXISTS idx_memory_entity_status
            ON memory_items(entity, status);
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_items_fts
            USING fts5(
                memory_id UNINDEXED,
                content,
                entity,
                category,
                tokenize='unicode61'
            );
        CREATE TABLE IF NOT EXISTS conversation_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT '',
            message_id TEXT NOT NULL DEFAULT '',
            reply_to_id TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            seq INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_conversation_turns_session_id
            ON conversation_turns(session_id, id);
        CREATE INDEX IF NOT EXISTS idx_conversation_turns_created_at
            ON conversation_turns(created_at);
        CREATE TABLE IF NOT EXISTS session_meta (
            session_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS session_working_state (
            session_id TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS provider_checkpoints (
            session_id TEXT PRIMARY KEY,
            messages_json TEXT NOT NULL DEFAULT '[]',
            summary TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            turn_id TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            seq INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_agent_events_session_id
            ON agent_events(session_id, id);
        CREATE TABLE IF NOT EXISTS transcript_seq_counters (
            session_id TEXT PRIMARY KEY,
            next_seq INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            turn_id TEXT NOT NULL DEFAULT '',
            phase TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cached_input_tokens INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_usage_events_session_id
            ON usage_events(session_id, id);
        CREATE TABLE IF NOT EXISTS fact_assertions (
            id TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            value_json TEXT NOT NULL,
            value_type TEXT NOT NULL DEFAULT 'string',
            scope TEXT NOT NULL DEFAULT 'global',
            source_kind TEXT NOT NULL DEFAULT 'manual_write',
            source_id TEXT NOT NULL DEFAULT '',
            source_session TEXT NOT NULL DEFAULT '',
            channel TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 1.0,
            status TEXT NOT NULL DEFAULT 'active',
            valid_from TEXT NOT NULL DEFAULT '',
            valid_to TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_fact_assertions_lookup
            ON fact_assertions(subject, predicate, scope, created_at, id);
        CREATE TABLE IF NOT EXISTS resolved_facts (
            fact_key TEXT PRIMARY KEY,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            value_json TEXT NOT NULL,
            value_type TEXT NOT NULL DEFAULT 'string',
            scope TEXT NOT NULL DEFAULT 'global',
            winning_assertion_id TEXT NOT NULL,
            resolution_reason TEXT NOT NULL DEFAULT 'resolved',
            confidence REAL NOT NULL DEFAULT 1.0,
            resolved_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_resolved_facts_lookup
            ON resolved_facts(subject, predicate, scope);
        """
    )
    # Order matters, and getting it wrong breaks every existing
    # install. ``CREATE TABLE IF NOT EXISTS`` is a no-op on a table that
    # already exists, so a database written before ``seq`` was
    # introduced keeps its old shape -- which is why the columns have to
    # be added before anything refers to them. The indexes on ``seq``
    # used to live in the script above and raised "no such column: seq"
    # on exactly those databases, before the ALTER below ever ran; a
    # fresh database passed, which is why only existing installs broke.
    migrations._add_missing_columns(conn)
    migrations._ensure_transcript_indexes(conn)
    migrations._backfill_transcript_seq(conn)


def _ensure_fts_index(conn) -> None:
    mismatch = conn.execute(
        """
        SELECT 1
        FROM (
            SELECT m.id AS memory_id
            FROM memory_items m
            WHERE m.status NOT IN ('archived', 'superseded')
            EXCEPT
            SELECT f.memory_id
            FROM memory_items_fts f
        )
        UNION ALL
        SELECT 1
        FROM (
            SELECT f.memory_id
            FROM memory_items_fts f
            EXCEPT
            SELECT m.id
            FROM memory_items m
            WHERE m.status NOT IN ('archived', 'superseded')
        )
        LIMIT 1
        """
    ).fetchone()
    if mismatch is None:
        return
    conn.execute("DELETE FROM memory_items_fts")
    rows = conn.execute(
        """
        SELECT id, content, entity, category
        FROM memory_items
        WHERE status NOT IN ('archived', 'superseded')
        """
    ).fetchall()
    conn.executemany(
        """
        INSERT INTO memory_items_fts (memory_id, content, entity, category)
        VALUES (?, ?, ?, ?)
        """,
        [
            (row["id"], row["content"], row["entity"], row["category"])
            for row in rows
        ],
    )

"""The one search the store does: a query against the entries' full-text index, with the category filter the caller asked for. Moved out of LTMStore (see the remaining-big-files survey)."""

from __future__ import annotations
import sqlite3

from agent import shared
from . import rows as _row_mappers, normalize
from ._helpers import _lexical_terms
from .models import LTMEntry

from agent.lexical import LATIN_TOKEN_RE

from typing import Any, Optional

__all__ = [
    "search_entries",
]


def search_entries(
    conn,
    query: str,
    categories: Optional[list[str]] = None,
    limit: int = shared.RETRIEVAL_TOP_K,
    scopes: Optional[list[str]] = None,
) -> list[LTMEntry]:
    if scopes is not None and not scopes:
        return []
    query_terms = _lexical_terms(query)
    if not query_terms:
        sql = """
                    SELECT * FROM memory_items
                    WHERE status NOT IN ('archived', 'superseded')
                """
        params: list[Any] = []
        if categories:
            cats = [normalize.normalize_category_name(c) for c in categories]
            sql += f" AND category IN ({','.join('?' for _ in cats)})"
            params.extend(cats)
        if scopes:
            sql += f" AND scope IN ({','.join('?' for _ in scopes)})"
            params.extend(str(scope) for scope in scopes)
        sql += " ORDER BY importance DESC, updated_at DESC, id ASC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [_row_mappers._row_to_entry(row) for row in rows]

    latin_terms = [term for term in query_terms if LATIN_TOKEN_RE.fullmatch(term)]
    cjk_terms = [term for term in query_terms if term not in latin_terms]
    results_by_id: dict[str, LTMEntry] = {}

    def _merge(rows: list[sqlite3.Row]) -> None:
        for row in rows:
            entry = _row_mappers._row_to_entry(row)
            results_by_id.setdefault(entry.id, entry)

    normalized_categories = (
        [normalize.normalize_category_name(c) for c in categories] if categories else []
    )

    if latin_terms:
        escaped_tokens = [token.replace('"', '""') for token in latin_terms]
        match_query = " OR ".join(f'"{token}"*' for token in escaped_tokens)
        sql = """
                    SELECT m.*
                    FROM memory_items_fts
                    JOIN memory_items AS m
                      ON m.id = memory_items_fts.memory_id
                    WHERE memory_items_fts MATCH ?
                      AND m.status NOT IN ('archived', 'superseded')
                """
        params: list[Any] = [match_query]
        if normalized_categories:
            sql += f" AND m.category IN ({','.join('?' for _ in normalized_categories)})"
            params.extend(normalized_categories)
        if scopes:
            sql += f" AND m.scope IN ({','.join('?' for _ in scopes)})"
            params.extend(str(scope) for scope in scopes)
        sql += """
                    ORDER BY bm25(memory_items_fts), m.importance DESC,
                             m.updated_at DESC, m.id ASC
                    LIMIT ?
                """
        params.append(limit * 6)
        _merge(conn.execute(sql, params).fetchall())

    if cjk_terms:
        like_clauses = []
        params = []
        for term in cjk_terms[:12]:
            pattern = f"%{term}%"
            like_clauses.append(
                "(content LIKE ? OR entity LIKE ? OR category LIKE ?)"
            )
            params.extend([pattern, pattern, pattern])
        sql = """
                    SELECT *
                    FROM memory_items
                    WHERE status NOT IN ('archived', 'superseded')
                """
        if like_clauses:
            sql += " AND (" + " OR ".join(like_clauses) + ")"
        if normalized_categories:
            sql += f" AND category IN ({','.join('?' for _ in normalized_categories)})"
            params.extend(normalized_categories)
        if scopes:
            sql += f" AND scope IN ({','.join('?' for _ in scopes)})"
            params.extend(str(scope) for scope in scopes)
        sql += """
                    ORDER BY importance DESC, updated_at DESC, id ASC
                    LIMIT ?
                """
        params.append(limit * 6)
        _merge(conn.execute(sql, params).fetchall())

    return list(results_by_id.values())[: limit * 6]

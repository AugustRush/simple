"""Provider-call accounting: what one session spent, and recording one call.

Moved out of ``LTMStore`` (see the remaining-big-files survey).  Both take the connection as
their first argument instead of opening one, which is the seam that let them leave: the store
keeps the thread-local connection -- and the commit that happens when its context manager
exits -- and these keep the SQL.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from ._helpers import _now

__all__ = ['append_usage_event', 'usage_summary']


def append_usage_event(conn,
    *,
    session_id: str,
    phase: str,
    model: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_input_tokens: int = 0,
    turn_id: str = "",
    metadata: Optional[dict[str, Any]] = None,
    created_at: Optional[str] = None,
) -> None:
    """Persist one provider call so foreground and background cost is visible."""

    conn.execute(
        """
        INSERT INTO usage_events (
            session_id, turn_id, phase, model, input_tokens,
            output_tokens, cached_input_tokens, metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(session_id or "").strip() or "default",
            str(turn_id or ""),
            str(phase or "foreground").strip() or "foreground",
            str(model or ""),
            max(0, int(input_tokens or 0)),
            max(0, int(output_tokens or 0)),
            max(0, int(cached_input_tokens or 0)),
            json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
            created_at or _now(),
        ),
    )


def usage_summary(conn, session_id: str) -> dict[str, Any]:
    """Return bounded aggregate usage for one session, grouped by phase."""

    clean = str(session_id or "").strip()
    if not clean:
        return {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                "cached_input_tokens": 0, "by_phase": {}}
    rows = conn.execute(
        """
        SELECT phase, COUNT(*) AS calls,
               SUM(input_tokens) AS input_tokens,
               SUM(output_tokens) AS output_tokens,
               SUM(cached_input_tokens) AS cached_input_tokens
        FROM usage_events WHERE session_id = ? GROUP BY phase
        """,
        (clean,),
    ).fetchall()
    by_phase = {
        str(row["phase"]): {
            "calls": int(row["calls"] or 0),
            "input_tokens": int(row["input_tokens"] or 0),
            "output_tokens": int(row["output_tokens"] or 0),
            "cached_input_tokens": int(row["cached_input_tokens"] or 0),
        }
        for row in rows
    }
    return {
        "calls": sum(item["calls"] for item in by_phase.values()),
        "input_tokens": sum(item["input_tokens"] for item in by_phase.values()),
        "output_tokens": sum(item["output_tokens"] for item in by_phase.values()),
        "cached_input_tokens": sum(
            item["cached_input_tokens"] for item in by_phase.values()
        ),
        "by_phase": by_phase,
    }

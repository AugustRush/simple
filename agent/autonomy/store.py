"""The proposals table: what the agent would have done, in its own database.

Separate file from ``agent/autonomy/__init__.py`` on purpose: the model of a
proposal is worth reading on its own, and the SQL is worth reading on its own.

Its own database file, not the scheduler's.  The scheduler's schema is versioned
and migrated on every start of every process; a table that exists to be
calibrated (and dropped, once the calibration is done) should not be welded to
it.  ``~/.agent/autonomy/proposals.db`` is opened the same way the scheduler's
store is, so a person can inspect it with ``sqlite3`` and nothing else.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from . import Proposal

__all__ = ["ProposalStore", "open_proposal_store"]

UTC = timezone.utc

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    tool TEXT NOT NULL DEFAULT '',
    reversibility TEXT NOT NULL DEFAULT 'irreversible',
    confidence REAL NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'proposed',
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_proposals_state
    ON proposals(state, created_at);
CREATE INDEX IF NOT EXISTS idx_proposals_source
    ON proposals(source, created_at);
"""


def _iso(value: Optional[datetime]) -> str:
    return value.astimezone(UTC).isoformat() if value is not None else ""


def _parse(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


class ProposalStore:
    """Read and write proposals.  No policy lives here: this is storage."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- lifetime ----------------------------------------------------------

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "ProposalStore":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- writes ------------------------------------------------------------

    def record(self, proposal: Proposal) -> bool:
        """Insert one proposal.  ``False`` when the same one is already live.

        Deduplication happens **here**, in the store, because it is a fact about
        the table rather than a policy about the loop: a suggestion already live
        for the same source and the same action is not a second suggestion.  A
        declined one does not block a new one -- the situation may genuinely
        have changed, and the *decline* is what the reader is told about.
        """
        row = proposal.to_row()
        with self._transaction() as conn:
            live = conn.execute(
                """
                SELECT id FROM proposals
                WHERE state = 'proposed'
                  AND source = ?
                  AND lower(trim(action)) = lower(trim(?))
                  AND expires_at > ?
                LIMIT 1
                """,
                (row["source"], row["action"], _iso(proposal.created_at)),
            ).fetchone()
            if live is not None:
                return False
            conn.execute(
                """
                INSERT INTO proposals (
                    id, source, reason, action, tool, reversibility, confidence,
                    state, context_json, created_at, expires_at, decided_at, decided_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"], row["source"], row["reason"], row["action"], row["tool"],
                    row["reversibility"], row["confidence"], row["state"],
                    json.dumps(row["context_json"], ensure_ascii=False),
                    row["created_at"], row["expires_at"], row["decided_at"],
                    row["decided_by"],
                ),
            )
        return True

    def decide(self, proposal_id: str, state: str, *, by: str = "") -> bool:
        """Move one proposal to a final state.

        Refuses to overwrite an existing decision: the record of *who said no*
        is the reason this table exists, and a later write must not erase it.
        """
        if state not in {"accepted", "declined", "expired", "acted"}:
            raise ValueError(f"not a final proposal state: {state}")
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE proposals
                SET state = ?, decided_at = ?, decided_by = ?
                WHERE id = ? AND state = 'proposed'
                """,
                (state, _iso(datetime.now(UTC)), by, proposal_id),
            )
            return cursor.rowcount > 0

    def expire_stale(self, now: Optional[datetime] = None) -> int:
        """Mark live proposals past their expiry.  Returns how many."""
        moment = _iso(now or datetime.now(UTC))
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE proposals
                SET state = 'expired', decided_at = ?, decided_by = 'time'
                WHERE state = 'proposed' AND expires_at <= ?
                """,
                (moment, moment),
            )
            return int(cursor.rowcount or 0)

    # -- reads -------------------------------------------------------------

    def _row_to_proposal(self, row: sqlite3.Row) -> Proposal:
        try:
            context = json.loads(row["context_json"] or "{}")
        except ValueError:
            context = {}
        return Proposal(
            id=str(row["id"]),
            source=str(row["source"]),
            reason=str(row["reason"]),
            action=str(row["action"]),
            tool=str(row["tool"]),
            reversibility=str(row["reversibility"]),
            confidence=float(row["confidence"] or 0.0),
            state=str(row["state"]),
            context=context if isinstance(context, dict) else {},
            created_at=_parse(row["created_at"]) or datetime.now(UTC),
            expires_at=_parse(row["expires_at"]) or datetime.now(UTC),
            decided_at=_parse(row["decided_at"]),
            decided_by=str(row["decided_by"]),
        )

    def list_proposals(
        self, *, state: Optional[str] = None, limit: int = 100
    ) -> list[Proposal]:
        with self._transaction() as conn:
            if state:
                rows = conn.execute(
                    "SELECT * FROM proposals WHERE state = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (state, int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM proposals ORDER BY created_at DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        return [self._row_to_proposal(row) for row in rows]

    def counts(self) -> dict[str, int]:
        """How many proposals in each state.  The number the page leads with."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM proposals GROUP BY state"
            ).fetchall()
        return {str(row["state"]): int(row["n"]) for row in rows}

    def already_decided(self, source: str, action: str) -> Optional[str]:
        """The state a matching proposal was last decided in, if any.

        This is the question a rephrasing model cannot answer about itself: *has
        somebody already said no to this*.  The answer is the point of keeping
        declined rows.
        """
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT state FROM proposals
                WHERE source = ? AND lower(trim(action)) = lower(trim(?))
                  AND state <> 'proposed'
                ORDER BY created_at DESC LIMIT 1
                """,
                (source, action),
            ).fetchone()
        return str(row["state"]) if row is not None else None


def open_proposal_store(home: Optional[Path] = None) -> ProposalStore:
    """``<agent home>/autonomy/proposals.db``."""
    if home is None:
        from agent import shared

        home = shared.AGENT_HOME
    return ProposalStore(Path(home) / "autonomy" / "proposals.db")

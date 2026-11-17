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
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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

-- One row per patrol pass.  The proposals table answers "what was found"; this
-- answers "how much was looked at to find it", which is the other half of the
-- calibration: a funnel that proposes nothing because it examines nothing looks
-- exactly like a funnel that proposes nothing because there is nothing to do.
CREATE TABLE IF NOT EXISTS patrol_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_at TEXT NOT NULL,
    examined INTEGER NOT NULL DEFAULT 0,
    eligible INTEGER NOT NULL DEFAULT 0,
    skipped_warm INTEGER NOT NULL DEFAULT 0,
    skipped_stale INTEGER NOT NULL DEFAULT 0,
    proposed INTEGER NOT NULL DEFAULT 0,
    dropped INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    expired INTEGER NOT NULL DEFAULT 0,
    -- How many of this pass's proposals actually ran without asking. The
    -- difference between "proposed 40, executed 0" and "proposed 40, executed 3"
    -- is whether the loop is doing anything, and only one of those numbers was
    -- being recorded.
    executed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_patrol_ticks_at
    ON patrol_ticks(tick_at);

-- What the agent decided to work towards on its own.  Three other tables look
-- adjacent and are each a different promise:
--
--   proposals   -- "here is something I found; may I?"  (the patrol's guess)
--   goals       -- "I intend to bring this about"       (its own intention)
--
-- A goal is not a scheduled task either: a task is an instruction with a time,
-- a goal is a desired state it may reach however it likes.  Keeping them
-- separate is what lets "it is working towards something" be a fact the board
-- can show without pretending it is a schedule.
CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    -- Why it decided this was worth doing.  Written for the person, not for the
    -- model: a goal with no stated reason is indistinguishable from a whim, and
    -- the person is the one who has to decide whether to trust it.
    why TEXT NOT NULL DEFAULT '',
    -- How it will know it is done.  Empty is allowed (some goals are "keep an
    -- eye on X"), but a goal that can never be checked can never be closed
    -- either, so the page says so when it is missing.
    done_when TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    -- How many times the patrol has proposed working on it.  Bounded, because a
    -- goal that keeps being proposed and never progresses is a goal that should
    -- be abandoned rather than retried forever.
    attempts INTEGER NOT NULL DEFAULT 0,
    source_session TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_goals_status
    ON goals(status, created_at);
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
        self._add_missing_columns()
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

    def get_proposal(self, proposal_id: str) -> Optional["Proposal"]:
        """One row by id, or ``None``.

        Needed by the accept path: the call to carry out is written on the row,
        and re-reading it from the store (rather than from the request) is what
        keeps a stale page from applying something else.
        """
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM proposals WHERE id = ?", (str(proposal_id or ""),)
            ).fetchone()
        return self._row_to_proposal(row) if row is not None else None

    def counts(self) -> dict[str, int]:
        """How many proposals in each state.  The number the page leads with."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM proposals GROUP BY state"
            ).fetchall()
        return {str(row["state"]): int(row["n"]) for row in rows}

    def _add_missing_columns(self) -> None:
        """Bring a database written by an earlier version up to this schema.

        ``CREATE TABLE IF NOT EXISTS`` does nothing to a table that already
        exists, so a column added later is missing from every install that has
        ever run -- and the first insert that names it fails with "no such
        column", on somebody else's machine. Additive columns only: a rename or a
        drop would need a real migration, and this file has not earned one.

        Runs on the connection that just applied the schema, before its commit:
        a second connection would take a write lock this one holds.
        """
        wanted = {"executed": "INTEGER NOT NULL DEFAULT 0"}
        try:
            present = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(patrol_ticks)").fetchall()
            }
            for column, ddl in wanted.items():
                if column not in present:
                    self._conn.execute(f"ALTER TABLE patrol_ticks ADD COLUMN {column} {ddl}")
        except sqlite3.Error:
            # An unreadable table is every reader's problem to tolerate, not this
            # one's to crash over: they all fall back to zero.
            pass

    def record_tick(self, result: dict[str, Any], *, at: Optional[datetime] = None) -> None:
        """Store one patrol pass.

        Not a log line: the numbers are what the *next* decision is calibrated
        from, and a log rotated out of existence cannot be queried.
        """
        moment = _iso(at or datetime.now(UTC))
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO patrol_ticks (
                    tick_at, examined, eligible, skipped_warm, skipped_stale,
                    proposed, dropped, duplicates, expired, executed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    moment,
                    int(result.get("examined", 0) or 0),
                    int(result.get("eligible", 0) or 0),
                    int(result.get("skipped_warm", 0) or 0),
                    int(result.get("skipped_stale", 0) or 0),
                    int(result.get("proposed", 0) or 0),
                    int(result.get("dropped", 0) or 0),
                    int(result.get("duplicates", 0) or 0),
                    int(result.get("expired", 0) or 0),
                    int(result.get("executed", 0) or 0),
                ),
            )

    #: How long the pass history is kept. Longer than the 7-day window the page
    #: reads, so a question about last month still has an answer, and short
    #: enough that the table stays a history rather than an archive.
    TICK_RETENTION_DAYS = 30

    def prune_ticks(
        self, *, now: Optional[datetime] = None, days: Optional[int] = None
    ) -> int:
        """Drop pass records older than the retention window.

        Nothing else deletes these: every pass appends a row, ``tick_stats``
        only ever reads a window, and measured on a real install 696 of 1609
        rows were already past the point where anything would look at them.

        Retention is by *time*, not by row count, and that is deliberate: a cap
        of N rows means "how far back can I see" silently changes with the poll
        interval -- 5000 rows is 35 days at a ten-minute pass and 3.5 days at a
        one-minute one. A window means the same thing either way.
        """
        moment = now or datetime.now(UTC)
        horizon = moment - timedelta(days=int(days if days is not None else self.TICK_RETENTION_DAYS))
        with self._transaction() as conn:
            removed = conn.execute(
                "DELETE FROM patrol_ticks WHERE tick_at < ?", (_iso(horizon),)
            ).rowcount
        return int(removed or 0)

    def tick_stats(self, since: Optional[datetime] = None) -> dict[str, Any]:
        """The funnel's numbers over a window: how much was looked at, how much came out.

        ``passes`` and the sums are read together, so "20 passes examined 240
        sessions" and "proposed 3" cannot disagree -- the same invariant the
        attention snapshot makes for the badge.
        """
        window_start = _iso(since) if since is not None else ""
        with self._transaction() as conn:
            if window_start:
                rows = conn.execute(
                    "SELECT * FROM patrol_ticks WHERE tick_at >= ? ORDER BY tick_at DESC",
                    (window_start,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM patrol_ticks ORDER BY tick_at DESC"
                ).fetchall()
        sums = {
            key: sum(int(row[key] or 0) for row in rows)
            for key in (
                "examined", "eligible", "skipped_warm", "skipped_stale",
                "proposed", "dropped", "duplicates", "expired", "executed",
            )
        }
        return {
            "passes": len(rows),
            "first_at": str(rows[-1]["tick_at"]) if rows else "",
            "last_at": str(rows[0]["tick_at"]) if rows else "",
            **sums,
        }

    def note_outcome(self, proposal_id: str, outcome: dict[str, Any]) -> bool:
        """Attach what the run did to the proposal that asked for it.

        Without this the loop is open: a proposal is acted on and nobody -- not
        the patrol, not the page -- can say whether acting on it was worth it.
        The verdict is the calibration's most important input: "proposed 40,
        acted 3, succeeded 1" is a different product from "proposed 40, acted
        3, succeeded 3".

        Stored in ``context_json`` rather than its own columns: the outcome is
        free-form (a run reports status, summary, output path), and a table that
        grows a column per field a run happens to report is a table that has to
        be migrated whenever the run grows one.
        """
        clean = str(proposal_id or "").strip()
        if not clean:
            return False
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT context_json FROM proposals WHERE id = ?", (clean,)
            ).fetchone()
            if row is None:
                return False
            try:
                context = json.loads(row["context_json"] or "{}")
            except ValueError:
                context = {}
            if not isinstance(context, dict):
                context = {}
            context["outcome"] = dict(outcome or {})
            conn.execute(
                "UPDATE proposals SET context_json = ? WHERE id = ?",
                (json.dumps(context, ensure_ascii=False), clean),
            )
        return True

    def awaiting_outcome(self, limit: int = 50) -> list[Any]:
        """Proposals that were acted on and whose run has not been read back yet."""
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM proposals WHERE state = 'acted' "
                "ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        proposals = [self._row_to_proposal(row) for row in rows]
        # "Queued" is a *placement*, not an outcome. The bridge writes the task id
        # and a queued status when it creates the task; treating that as a result
        # means the loop never reads the run it created -- found by a test that
        # finished a run and got zero read-backs.
        terminal = {"succeeded", "failed", "interrupted", "cancelled", "skipped", "unverified"}
        return [
            item for item in proposals
            if not (
                isinstance(item.context, dict)
                and isinstance(item.context.get("outcome"), dict)
                and str(item.context["outcome"].get("status") or "") in terminal
            )
        ]

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


    # -- goals: what it decided to work towards on its own -----------------

    #: How many times the patrol may propose continuing one goal before it is
    #: abandoned. Bounded on purpose: a goal that keeps being offered and never
    #: moves is not persistence, it is a loop that has stopped being useful.
    MAX_GOAL_ATTEMPTS = 3

    def set_goal(
        self,
        goal: str,
        *,
        why: str = "",
        done_when: str = "",
        source_session: str = "",
        now: Optional[datetime] = None,
    ) -> str:
        """Record one intention of the agent's own. Returns its id.

        The same goal twice is one goal: an agent that restates its intention
        every turn would fill the list with copies of itself, and the board
        would read as "it is doing three things" when it is doing one.
        """
        text = " ".join(str(goal or "").split())
        if not text:
            raise ValueError("goal must not be empty")
        moment = _iso(now or datetime.now(UTC))
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT id FROM goals WHERE status = 'open' AND lower(trim(goal)) = lower(trim(?))",
                (text,),
            ).fetchone()
            if existing is not None:
                # Refreshing the reason is the useful half of a restatement; the
                # attempt count stays, because restating a goal is not progress.
                conn.execute(
                    "UPDATE goals SET why = ?, done_when = ?, updated_at = ? WHERE id = ?",
                    (
                        str(why or "").strip(),
                        str(done_when or "").strip(),
                        moment,
                        existing["id"],
                    ),
                )
                return str(existing["id"])
            goal_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO goals (id, goal, why, done_when, status, attempts,
                                   source_session, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'open', 0, ?, ?, ?)
                """,
                (
                    goal_id,
                    text,
                    str(why or "").strip(),
                    str(done_when or "").strip(),
                    str(source_session or ""),
                    moment,
                    moment,
                ),
            )
        return goal_id

    def list_goals(self, *, status: str = "open") -> list[dict[str, Any]]:
        """Goals in one state, oldest first (the thing waiting longest goes first)."""
        with self._transaction() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM goals WHERE status = ? ORDER BY created_at ASC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM goals ORDER BY created_at ASC"
                ).fetchall()
        return [dict(row) for row in rows]

    def close_goal(
        self,
        goal_id: str,
        *,
        status: str = "done",
        now: Optional[datetime] = None,
    ) -> bool:
        """Finish or abandon one goal. ``False`` when it was not open."""
        if status not in ("done", "abandoned"):
            raise ValueError(f"not a closing status: {status}")
        moment = _iso(now or datetime.now(UTC))
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE goals SET status = ?, closed_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'open'",
                (status, moment, moment, str(goal_id or "").strip()),
            ).rowcount
        return bool(changed)

    def note_goal_attempt(
        self, goal_id: str, *, now: Optional[datetime] = None
    ) -> int:
        """Count one more proposal to continue this goal; abandon it at the cap.

        Returns the attempt count afterwards. The abandonment is *not* silent --
        the caller reports it, because "I stopped trying" is exactly the kind of
        thing an autonomous agent must say out loud rather than let fade.
        """
        moment = _iso(now or datetime.now(UTC))
        with self._transaction() as conn:
            conn.execute(
                "UPDATE goals SET attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (moment, str(goal_id or "").strip()),
            )
            row = conn.execute(
                "SELECT attempts FROM goals WHERE id = ?", (str(goal_id or "").strip(),)
            ).fetchone()
            attempts = int(row["attempts"]) if row is not None else 0
            if attempts >= self.MAX_GOAL_ATTEMPTS:
                conn.execute(
                    "UPDATE goals SET status = 'abandoned', closed_at = ?, updated_at = ? "
                    "WHERE id = ? AND status = 'open'",
                    (moment, moment, str(goal_id or "").strip()),
                )
        return attempts

def open_proposal_store(home: Optional[Path] = None) -> ProposalStore:
    """``<agent home>/autonomy/proposals.db``."""
    if home is None:
        from agent import shared

        home = shared.AGENT_HOME
    return ProposalStore(Path(home) / "autonomy" / "proposals.db")

"""What the agent is already on the hook for, put in front of it every turn.

Why this exists
---------------
Three records in this system describe work that is not finished:

* **commitments** — "I told you I would do this", owed to a person, at a time.
* **goals** — "I intend to bring this about", the agent's own intention.
* **unfinished runs** — already surfaced by the scheduler's own reports.

The first two were *write-only*. They were recorded faithfully, shown on the
board, followed up by the patrol -- and never mentioned to the agent itself. So
a promise made on Monday was invisible on Tuesday, and a goal it set for itself
was something it could only rediscover by calling `list_goals`, which it had no
reason to do. A goal nobody remembers is not an intention; a promise nobody
remembers is how you break one.

Why it rides in the turn's message and not the system prompt
------------------------------------------------------------
The same reason the current time does (see ``turn_loop``): the system prompt is
the head of the request, so anything volatile there caps what the provider can
serve from its prefix cache for the rest of the session. This block changes
whenever a commitment is kept or a goal is closed.

What it deliberately does not say
---------------------------------
No instruction to act. The block states what is outstanding and asks the agent
to keep it in view; whether to work on it now is the agent's judgement, and a
line that said "do these next" would turn a reminder into a queue that outranks
whatever the person actually asked for in this turn.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

__all__ = ["outstanding_block"]

logger = logging.getLogger(__name__)

#: How many of each to show. A prompt block is read, not searched: ten lines is
#: a reminder, fifty is a document nobody finishes. The board holds the rest.
MAX_SHOWN = 5


def outstanding_block(
    *,
    commitment_store: Optional[Any] = None,
    goal_store: Optional[Any] = None,
) -> str:
    """The block, or ``""`` when nothing is outstanding.

    Both stores are optional and both are read best-effort: a composition
    without a scheduler, or a store that cannot be opened, means "nothing to
    remind about" rather than a turn that fails to start.
    """
    commitments = _commitments(commitment_store)
    goals = _goals(goal_store)
    if not commitments and not goals:
        return ""

    lines = [
        "## Outstanding",
        "Work you already have in hand, from earlier turns. It is here so it does "
        "not get lost — not as a queue that outranks what the person asks for now.",
    ]
    if commitments:
        lines.append("")
        lines.append("Promises you made to the person:")
        for item in commitments:
            when = item.get("due_text") or ""
            said = item.get("said") or ""
            goal = item.get("goal") or ""
            suffix = f"（{when}）" if when else ""
            said_note = f" —— 你当时说的是「{said}」" if said else ""
            lines.append(f"- {goal}{suffix}{said_note}")
    if goals:
        lines.append("")
        lines.append("Goals you set for yourself:")
        for item in goals:
            why = item.get("why") or ""
            why_note = f" —— 因为 {why}" if why else ""
            lines.append(f"- {item.get('goal') or ''}{why_note}")
    lines.append("")
    lines.append(
        "If one of these is done or no longer worth doing, close it "
        "(`close_goal`, or the promise's task) so the list stays worth reading."
    )
    return "\n".join(lines)


def _commitments(store: Optional[Any]) -> list[dict[str, Any]]:
    if store is None:
        try:
            from agent.scheduler import open_store

            store = open_store()
        except Exception:
            return []
    try:
        from agent.tools.schedule_tools import open_commitments

        tasks = open_commitments(store)
    except Exception:
        logger.debug("outstanding: could not read commitments", exc_info=True)
        return []
    rows: list[dict[str, Any]] = []
    for task in tasks[:MAX_SHOWN]:
        payload = getattr(task, "payload", None) or {}
        commitment = payload.get("commitment") if isinstance(payload, dict) else {}
        rows.append(
            {
                "goal": str((payload or {}).get("prompt") or "")[:120],
                "said": str((commitment or {}).get("said") or "")[:120],
                "due_text": _due_text(getattr(task, "next_run_at", None)),
            }
        )
    return rows


def _due_text(due: Any) -> str:
    from datetime import datetime, timezone

    if due is None:
        return ""
    try:
        moment = due if isinstance(due, datetime) else datetime.fromisoformat(str(due))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    seconds = (moment - datetime.now(timezone.utc)).total_seconds()
    if seconds <= 0:
        return "已经到点了"
    if seconds < 3600:
        return f"约 {max(1, round(seconds / 60))} 分钟后"
    if seconds < 86400:
        return f"约 {max(1, round(seconds / 3600))} 小时后"
    return moment.astimezone().strftime("%m-%d %H:%M")


def _goals(store: Optional[Any]) -> list[dict[str, Any]]:
    if store is None:
        try:
            from agent.autonomy.store import open_proposal_store

            store = open_proposal_store()
        except Exception:
            return []
    try:
        rows = store.list_goals()
    except Exception:
        logger.debug("outstanding: could not read goals", exc_info=True)
        return []
    return [
        {
            "goal": str(row.get("goal") or "")[:120],
            "why": str(row.get("why") or "")[:120],
        }
        for row in rows[:MAX_SHOWN]
    ]

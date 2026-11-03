"""The home page's one request: what needs a person, what is running, what happened.

Why one endpoint instead of five
--------------------------------
The page opens with the question "is anything waiting for me, and what has this
agent been doing". Answering it from the client would mean five requests, and --
worse -- five places computing what "needs a person" means. Two of them would
drift: the badge in the sidebar already counts unacknowledged failures, and a
home page that recounted them its own way would disagree with the number right
above it in the same window.

So the decision lives here, once. The client renders it.

Why the activity list mixes conversations and runs
--------------------------------------------------
Because the question is "what has it done lately", and a conversation is one of
the things it did. Splitting them into "runs" and "threads" would answer a
question about the *schema*, not about the agent -- and the rhythm that makes the
list readable ("we talked in the morning, tasks ran in the afternoon") only shows
up when they are sorted together.

What it deliberately does not include
-------------------------------------
Counts of things that are fine. A healthy agent produces an empty `needs` block and
an empty `running` block, and the page is then a box and a history -- which is the
correct amount of screen for "nothing is wrong".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

__all__ = ["_home_overview", "build_home_overview"]

UTC = timezone.utc

#: How many items the activity timeline carries. Enough to show a day's shape, few
#: enough that nobody scrolls a home page.
ACTIVITY_LIMIT = 8


def _unavailable() -> Any:
    from starlette.responses import JSONResponse

    return JSONResponse({"error": "home is not available"}, status_code=503)


async def _home_overview(
    request: Any, *, authorized, scheduler_store, proposal_store, sessions=None
) -> Any:
    from starlette.responses import JSONResponse

    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = build_home_overview(
            scheduler_store=scheduler_store,
            proposal_store=proposal_store,
            sessions=sessions,
        )
    except Exception as exc:
        return JSONResponse({"error": f"读取主页失败：{exc}"}, status_code=500)
    return JSONResponse(payload)


def build_home_overview(
    *,
    scheduler_store: Any = None,
    proposal_store: Any = None,
    sessions: Optional[list[dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """The page's payload, from the stores that hold each fact.

    Every part is optional: a home page with no scheduler says "nothing is
    scheduled" rather than failing, because "this agent does not run tasks" is a
    configuration, not an error.
    """
    moment = now or datetime.now(UTC)
    return {
        "needs": _needs(scheduler_store, proposal_store),
        "board": _board(scheduler_store, proposal_store=proposal_store, now=moment),
        "activity": _activity(scheduler_store, sessions, moment=moment),
        "at": moment.isoformat(),
    }


# ── what needs a person ────────────────────────────────────────────────────


def _needs(scheduler_store: Any, proposal_store: Any) -> dict[str, Any]:
    """Two kinds of waiting, and both are decisions nobody else can make.

    A proposal waits for an answer; a failed run waits for somebody to look. They
    are different acts but the same claim on attention, so they travel together.
    """
    proposals = 0
    try:
        if proposal_store is not None:
            proposals = int(proposal_store.counts().get("proposed", 0) or 0)
    except Exception:
        proposals = 0

    failures = 0
    failure_rows: list[dict[str, Any]] = []
    try:
        if scheduler_store is not None:
            snapshot = scheduler_store.attention_snapshot()
            failures = int(snapshot.get("total", 0) or 0)
            # Only the newest one: the page says "2 次运行结束得不好" and names the
            # most recent. A list of them belongs on the page that can act on them.
            failure_rows = [
                {
                    "run_id": str(getattr(run, "id", "") or ""),
                    "task_id": str(getattr(run, "task_id", "") or ""),
                    "status": str(getattr(run, "status", "") or ""),
                }
                for run in list(snapshot.get("runs") or [])[:1]
            ]
    except Exception:
        failures = 0
        failure_rows = []

    return {
        "proposals": proposals,
        "failures": failures,
        "newest_failure": failure_rows[0] if failure_rows else None,
    }


# ── the board: what is running, what is waiting, what is next ─────────────


#: How many scheduled cards the board shows before saying "还有 N 个".
#:
#: A board is glanced at. Thirty cards in the third column is a list wearing a
#: board's clothes -- the value of the column is "nothing surprising is coming",
#: and that is answered by the three soonest plus a count.
BOARD_SCHEDULED_PREVIEW = 3


def _board(
    scheduler_store: Any, *, proposal_store: Any = None, now: datetime
) -> dict[str, Any]:
    """Three columns: 正在跑 / 等你处理 / 已排期.

    Why these three and not four
    ----------------------------
    "已完成" is not a column here: finished work that needs nobody is the
    *timeline*'s job (see ``_activity``), and the difference between a board and a
    log is exactly that the board holds only what is still open. A board with a
    "done" column is a log with worse scanning.

    Why the order is running -> waiting -> waiting-on-you is *not* the order
    ------------------------------------------------------------------------
    The columns are ordered by how distant they are from needing a person:
    something running needs nothing, something scheduled needs nothing yet, and
    something that failed needs *you*. Reading left to right is reading from
    "ignore" to "act", which is the direction attention actually travels.

    Two queries, not N
    ------------------
    One ``list_tasks`` and one ``recent_runs``, then a group-by in memory. The
    per-task ``list_runs`` version was a query per card, and a board is read on
    every visit to the landing page.
    """
    empty: dict[str, Any] = {
        "running": [],
        "waiting": [],
        "scheduled": [],
        "scheduled_total": 0,
    }
    if scheduler_store is None:
        return empty
    try:
        tasks = {task.id: task for task in scheduler_store.list_tasks()}
        recent = scheduler_store.recent_runs(limit=200)
    except Exception:
        return empty

    # The newest run of each task. `recent_runs` leads with the running ones and
    # then orders by finish time, so the first occurrence per task is its latest
    # -- which is the one the card is about.
    latest_by_task: dict[str, Any] = {}
    for run, _task in recent:
        latest_by_task.setdefault(str(run.task_id), run)

    running: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    scheduled: list[dict[str, Any]] = []

    # Proposals first in the waiting column: a proposal is *blocked* (it will not
    # run without an answer), while a failed run has already happened and is only
    # waiting to be read. Blocked work outranks read-later work.
    waiting.extend(_proposal_cards(proposal_store))

    for task_id, task in tasks.items():
        if not getattr(task, "enabled", True):
            # A paused task is not "next": it will not run until somebody says so,
            # and showing it in the scheduled column would be a promise it cannot
            # keep. It is visible on 自动化, which is where it can be resumed.
            continue
        latest = latest_by_task.get(task_id)
        status = str(getattr(latest, "status", "") or "") if latest is not None else ""
        card = _card(task, latest)

        if status in {"queued", "running"} or getattr(task, "active_run_id", None):
            running.append(card)
        elif status in {"failed", "interrupted"} and getattr(
            latest, "acknowledged_at", None
        ) is None:
            # Failed *and* nobody has looked. The acknowledgement is what makes a
            # failure stop waiting: acknowledging is the act of having read it,
            # and a board that kept showing read failures would teach people to
            # ignore the column.
            waiting.append(card)
        elif getattr(task, "next_run_at", None) is not None:
            scheduled.append(card)

    running.sort(key=lambda item: item.get("at") or "")
    # Oldest first in both: the thing that has been waiting longest is the one to
    # deal with first. Proposals carry `created_at` as their `at`, so the two
    # kinds sort together by how long they have been waiting.
    waiting.sort(key=lambda item: item.get("at") or "")
    scheduled.sort(key=lambda item: item.get("at") or "")

    return {
        "running": running,
        "waiting": waiting,
        "scheduled": scheduled[:BOARD_SCHEDULED_PREVIEW],
        "scheduled_total": len(scheduled),
    }


def _proposal_cards(proposal_store: Any) -> list[dict[str, Any]]:
    """Unanswered proposals, as board cards.

    A proposal is not a task -- it is a *candidate* task, blocked on a person --
    so it uses the board's card shape without pretending to have a run status.
    ``status`` is empty and ``kind`` is ``proposal``; the renderer shows the
    answer buttons for that kind and a status line for the others.
    """
    if proposal_store is None:
        return []
    try:
        proposals = proposal_store.list_proposals(state="proposed", limit=10)
    except Exception:
        return []
    cards: list[dict[str, Any]] = []
    for proposal in proposals:
        cards.append(
            {
                "task_id": "",  # not a task yet: `id` is the proposal's id
                "id": str(getattr(proposal, "id", "") or ""),
                "name": str(getattr(proposal, "action", "") or ""),
                "kind": "proposal",
                "status": "",
                "at": (
                    proposal.created_at.isoformat()
                    if getattr(proposal, "created_at", None)
                    else ""
                ),
                "at_kind": "created",
                "trigger_type": "",
                # The reason *is* the card: a person deciding on a proposal needs
                # to know what was observed, and the action alone does not say.
                "detail": str(getattr(proposal, "reason", "") or "")[:160],
                "session_id": str(getattr(proposal, "source", "") or ""),
                "reversibility": str(getattr(proposal, "reversibility", "") or ""),
            }
        )
    return cards


def _card(task: Any, latest: Any) -> dict[str, Any]:
    """One board card: what it is, how it is doing, and when.

    ``at`` means different things in different columns -- when it started for a
    running card, when it failed for a waiting one, when it will fire for a
    scheduled one. One field rather than three because a card is rendered by one
    component, and three optional timestamps would make every card carry two
    nulls it never uses. ``at_kind`` says which it is, so the label can be honest.
    """
    status = str(getattr(latest, "status", "") or "") if latest is not None else ""
    started = getattr(latest, "started_at", None) if latest is not None else None
    finished = getattr(latest, "finished_at", None) if latest is not None else None
    next_run = getattr(task, "next_run_at", None)

    if status in {"queued", "running"}:
        at, at_kind = started, "started"
    elif status in {"failed", "interrupted"}:
        at, at_kind = finished or started, "finished"
    else:
        at, at_kind = next_run, "next"

    return {
        "task_id": str(getattr(task, "id", "") or ""),
        "name": str(getattr(task, "name", "") or ""),
        "kind": str(getattr(task, "kind", "") or ""),
        "status": status,
        "at": at.isoformat() if at is not None else "",
        "at_kind": at_kind,
        "trigger_type": str(getattr(getattr(task, "trigger", None), "trigger_type", "") or ""),
        # The line under the title. A failure explains itself; everything else is
        # silent, because a card that always carries a sentence becomes noise.
        "detail": (
            str(getattr(latest, "error", "") or getattr(latest, "summary", "") or "")[:160]
            if status in {"failed", "interrupted"}
            else ""
        ),
        "session_id": str(getattr(task, "session_id", "") or ""),
    }


# ── what happened ──────────────────────────────────────────────────────────


def _activity(
    scheduler_store: Any,
    sessions: Optional[list[dict[str, Any]]],
    *,
    moment: datetime,
) -> list[dict[str, Any]]:
    """One timeline: runs and conversations, newest first.

    Both carry "when" and "what", so they sort together. A run says how it ended;
    a conversation says how much was said. Neither is padded into the other's
    shape -- the ``kind`` field is what tells a renderer which facts exist.
    """
    items: list[dict[str, Any]] = []

    #: The timeline is "what happened", so a run that has not finished has not
    #: happened yet. It is on the board (that is what the board is for), and
    #: listing it here too made the same three in-flight tasks appear twice on the
    #: same page -- once as work, once as history, in the same visit.
    _still_running = {"queued", "running"}

    try:
        if scheduler_store is not None:
            for run, task in scheduler_store.recent_runs(limit=ACTIVITY_LIMIT * 2):
                # Prefer when it finished; a running one has no finish, so its start
                # is the most recent thing true about it.
                if str(getattr(run, "status", "") or "") in _still_running:
                    continue
                stamp = getattr(run, "finished_at", None) or getattr(run, "started_at", None)
                items.append(
                    {
                        "kind": "run",
                        "at": stamp.isoformat() if stamp else "",
                        "title": str(getattr(task, "name", "") or ""),
                        "status": str(getattr(run, "status", "") or ""),
                        "summary": str(getattr(run, "summary", "") or "")[:160],
                        "error": str(getattr(run, "error", "") or "")[:160],
                        "task_id": str(getattr(task, "id", "") or ""),
                        "run_id": str(getattr(run, "id", "") or ""),
                        "session_id": str(getattr(task, "session_id", "") or ""),
                    }
                )
    except Exception:
        pass

    for session in sessions or []:
        raw = str(session.get("last_activity") or "")
        if not raw:
            continue
        items.append(
            {
                "kind": "thread",
                "at": raw,
                "title": str(session.get("title") or "未命名线程"),
                "session_id": str(session.get("session_id") or ""),
                "turn_count": int(session.get("turn_count") or 0),
            }
        )

    # Sort on the parsed timestamp: the two sources format their times differently
    # (one from the scheduler's ISO writer, one from the session index), so
    # comparing the strings would order them by coincidence of format.
    def _key(item: dict[str, Any]) -> float:
        stamp = _parse(item.get("at"))
        return stamp.timestamp() if stamp else 0.0

    items.sort(key=_key, reverse=True)
    return items[:ACTIVITY_LIMIT]


def _parse(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

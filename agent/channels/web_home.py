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

__all__ = ["_activity_feed", "_home_overview", "build_home_overview"]

UTC = timezone.utc

#: How many items the activity timeline carries. Enough to show a day's shape, few
#: enough that nobody scrolls a home page.
ACTIVITY_LIMIT = 8


def _unavailable() -> Any:
    from starlette.responses import JSONResponse

    return JSONResponse({"error": "home is not available"}, status_code=503)


async def _home_overview(
    request: Any,
    *,
    authorized,
    scheduler_store,
    proposal_store,
    sessions=None,
    work_store=None,
) -> Any:
    from starlette.responses import JSONResponse

    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        payload = build_home_overview(
            scheduler_store=scheduler_store,
            proposal_store=proposal_store,
            sessions=sessions,
            work_store=work_store,
        )
    except Exception as exc:
        return JSONResponse({"error": f"读取主页失败：{exc}"}, status_code=500)
    return JSONResponse(payload)


async def _activity_feed(
    request: Any, *, authorized, scheduler_store, sessions=None
) -> Any:
    """Older activity, for the "加载更早" link under the home timeline.

    A cursor rather than a page number: the feed grows at the *front*, so an
    offset would repeat or skip rows whenever a run finished between two page
    loads -- which is exactly what happens while somebody is reading it.
    """
    from starlette.responses import JSONResponse

    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    before = str(request.query_params.get("before") or "").strip() or None
    try:
        limit = max(1, min(50, int(str(request.query_params.get("limit") or "20"))))
    except ValueError:
        limit = 20
    try:
        items = _activity(
            scheduler_store, sessions, moment=datetime.now(UTC), before=before, limit=limit
        )
    except Exception as exc:
        return JSONResponse({"error": f"读取活动失败：{exc}"}, status_code=500)
    return JSONResponse(
        {
            "activity": items,
            # The next cursor is the oldest item on this page. Empty when the page
            # came back short, which is how the client knows to stop asking.
            "next_before": items[-1]["at"] if len(items) >= limit else "",
        }
    )


def build_home_overview(
    *,
    scheduler_store: Any = None,
    proposal_store: Any = None,
    sessions: Optional[list[dict[str, Any]]] = None,
    work_store: Any = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """The page's payload, from the stores that hold each fact.

    Every part is optional: a home page with no scheduler says "nothing is
    scheduled" rather than failing, because "this agent does not run tasks" is a
    configuration, not an error.
    """
    moment = now or datetime.now(UTC)
    activity = _activity(scheduler_store, sessions, moment=moment)
    board = _board(scheduler_store, proposal_store=proposal_store, now=moment)
    # What it is working *on*, as opposed to what it is running: a Ralph goal
    # lives across turns and has no run row until one is executing. Without this
    # the panel answered "is anything happening" from runs alone, so a goal being
    # worked on looked like nothing at all.
    board["working"] = _working(work_store)
    return {
        "needs": _needs(scheduler_store, proposal_store),
        "board": board,
        "activity": activity,
        # A full first page means there *may* be more, so the page needs a cursor
        # to ask with. Without this the "加载更早" link could never appear on the
        # first visit -- the client had no cursor until after it had already
        # loaded a second page, which it could not ask for.
        "activity_next": activity[-1]["at"] if len(activity) >= ACTIVITY_LIMIT else "",
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
        # Unread failures per task, not just the newest one. The sidebar badge
        # counts *runs* that nobody has read; a column that only showed tasks
        # whose latest run failed disagreed with that number -- and made the
        # other failures unreachable from the page that is supposed to be the
        # answer to "is anything waiting for me".
        unread_by_task = {
            str(key): int(value or 0)
            for key, value in (scheduler_store.attention_snapshot().get("counts") or {}).items()
        }
    except Exception:
        return empty

    # The newest run of each task. `recent_runs` leads with the running ones and
    # then orders by finish time, so the first occurrence per task is its latest
    # -- which is the one the card is about.
    latest_by_task: dict[str, Any] = {}
    # The newest *unread failure* of each task. A waiting card is about that run,
    # not about the task's latest one: a task whose retry succeeded has a waiting
    # failure from yesterday, and a card that showed its next run would answer
    # "when will this happen" when the reader asked "what went wrong".
    unread_failure_by_task: dict[str, Any] = {}
    for run, _task in recent:
        task_key = str(run.task_id)
        latest_by_task.setdefault(task_key, run)
        if str(getattr(run, "status", "") or "") in {"failed", "interrupted"} and (
            getattr(run, "acknowledged_at", None) is None
        ):
            unread_failure_by_task.setdefault(task_key, run)

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
        unread = int(unread_by_task.get(task_id, 0) or 0)
        card = _card(
            task,
            latest,
            unread=unread,
            failure=unread_failure_by_task.get(task_id),
        )

        if status in {"queued", "running"} or getattr(task, "active_run_id", None):
            running.append(card)
        elif unread > 0:
            # Any *unread* failure, not only a failed latest run. Acknowledging is
            # the act of having read it, so a failure stops waiting when somebody
            # reads it -- not when a later run happens to succeed. A board that
            # kept showing read failures would teach people to ignore the column;
            # one that dropped unread failures because a retry worked would hide
            # the very thing the badge counts.
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


def _detail(latest: Any, *, failure: Any, status: str, unread: int) -> str:
    if failure is not None:
        return _human_failure(failure)
    if latest is None:
        return ""
    if status in {"failed", "interrupted"}:
        return _human_failure(latest)
    if unread > 0:
        return f"{unread} 次运行结束得不好，还没有人看过"
    return ""


#: How many running goals the board shows. A board is glanced at, and a goal
#: list is a queue -- the number is what says there is more.
BOARD_WORKING_PREVIEW = 3


def _working(work_store: Any) -> list[dict[str, Any]]:
    """The goals it is in the middle of, as board rows.

    Only `running` ones: a finished goal is a result, and results are the
    conversation's business (they are reported there). What the panel has to
    answer is "is it doing something right now", and a completed task is not
    doing anything.

    Nothing here is clickable: a Ralph task has no page of its own, and a row
    that opens nothing is worse than a row that says so. The goal text *is* the
    information.
    """
    if work_store is None:
        return []
    try:
        tasks = work_store.list_tasks()
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for task in tasks:
        if str(getattr(getattr(task, "status", ""), "value", "") or "") != "running":
            continue
        rows.append(
            {
                "id": str(getattr(task, "id", "") or ""),
                "goal": str(getattr(task, "goal", "") or "")[:120],
                "iteration": int(getattr(task, "current_iteration", 0) or 0),
                "max_iterations": int(getattr(task, "max_iterations", 0) or 0),
                "at": str(getattr(task, "created_at", "") or ""),
            }
        )
    rows.sort(key=lambda item: item.get("at") or "")
    return rows[:BOARD_WORKING_PREVIEW]


def _human_failure(row: Any, limit: int = 90) -> str:
    """A card's one line: what it means, not what the provider said.

    Measured on the board: `OpenDesign: connection lost mid-reply (unexpected
    EOF)` was the second line of an attention card for two days. The
    classification says "连接中断" and what to do about it; the original is one
    click away in the run detail.
    """
    from agent.scheduler.diagnostics import describe_failure

    reason = describe_failure(getattr(row, "error", ""))
    if reason is not None:
        return reason.summary[:limit]
    return str(getattr(row, "summary", "") or "")[:limit]


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
                # Which task this is about, when it is about one. A proposal
                # whose action is "decide what to do with 汇报" is useless
                # without a way to the task it names -- the person would have to
                # retype the name into 自动化.
                "target_task_id": str(
                    (getattr(proposal, "context", {}) or {}).get("task_id") or ""
                ),
                "unread": 0,
            }
        )
    return cards


def _card(
    task: Any, latest: Any, *, unread: int = 0, failure: Any = None
) -> dict[str, Any]:
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
    elif failure is not None:
        # This card is waiting on a person, so its time is the failure's -- even
        # when a later run succeeded. Showing the next run instead ("22 小时后")
        # would answer a question nobody asked while hiding the one being asked.
        at, at_kind = (
            getattr(failure, "finished_at", None) or getattr(failure, "started_at", None),
            "finished",
        )
    elif status in {"failed", "interrupted"}:
        at, at_kind = finished or started, "finished"
    else:
        at, at_kind = next_run, "next"

    # The status describes the run the card is about, which for a waiting card is
    # the unread failure -- not the retry that replaced it. A card reading
    # "成功" in the column called "等你处理" is the page contradicting itself.
    shown = failure if failure is not None else latest
    shown_status = str(getattr(shown, "status", "") or "")

    return {
        "task_id": str(getattr(task, "id", "") or ""),
        "name": str(getattr(task, "name", "") or ""),
        "kind": str(getattr(task, "kind", "") or ""),
        "status": shown_status,
        "at": at.isoformat() if at is not None else "",
        "at_kind": at_kind,
        "trigger_type": str(getattr(getattr(task, "trigger", None), "trigger_type", "") or ""),
        # The line under the title. A failure explains itself; everything else is
        # silent, because a card that always carries a sentence becomes noise.
        # What the card says under the title. A failed run explains itself; a task
        # whose *earlier* failures are unread while its latest run succeeded has
        # no failing run to quote, so it says how many there are instead.
        "detail": _detail(latest, failure=failure, status=status, unread=unread),
        "unread": unread,
        "session_id": str(getattr(task, "session_id", "") or ""),
    }


# ── what happened ──────────────────────────────────────────────────────────


def _activity(
    scheduler_store: Any,
    sessions: Optional[list[dict[str, Any]]],
    *,
    moment: datetime,
    before: Optional[str] = None,
    limit: int = ACTIVITY_LIMIT,
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
            for run, task in scheduler_store.recent_runs(
                limit=max(limit * 2, ACTIVITY_LIMIT * 2), before=before
            ):
                # Prefer when it finished; a running one has no finish, so its start
                # is the most recent thing true about it.
                if str(getattr(run, "status", "") or "") in _still_running:
                    continue
                stamp = getattr(run, "finished_at", None) or getattr(run, "started_at", None)
                # An errand the patrol started for itself is *the agent acting*,
                # so the row says so. The name is the action sentence already;
                # provenance is a field, not something a reader parses out of it.
                from agent.autonomy import is_own_errand

                own = is_own_errand(getattr(task, "payload", None))
                items.append(
                    {
                        "kind": "run",
                        "at": stamp.isoformat() if stamp else "",
                        "title": str(getattr(task, "name", "") or ""),
                        "origin": "autonomy" if own else "",
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
        # A page asking for what happened *before* a moment must not be handed
        # conversations from after it: the cursor applies to both kinds, or the
        # second page repeats the first one's threads.
        if before and raw >= before:
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
    return items[:limit]


def _parse(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

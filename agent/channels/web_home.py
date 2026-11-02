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
        "running": _running(scheduler_store),
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


# ── what is happening now ──────────────────────────────────────────────────


def _running(scheduler_store: Any) -> dict[str, Any]:
    """Runs in flight, and what is next -- two different tenses of "busy".

    ``in_flight`` is the backend's answer (see ``web_payloads.task_in_flight``),
    not something derived here: a signal-woken run is written down as ``queued``
    with no ``active_run_id`` until a tick claims it, so a reader that looked at
    ``active_run_id`` alone would report "nothing running" in exactly the window
    somebody is watching.
    """
    in_flight: list[dict[str, Any]] = []
    next_up: list[dict[str, Any]] = []
    try:
        if scheduler_store is None:
            return {"in_flight": [], "next_up": []}
        tasks = list(scheduler_store.list_tasks())
    except Exception:
        return {"in_flight": [], "next_up": []}

    for task in tasks:
        if not getattr(task, "enabled", True):
            continue
        try:
            runs = scheduler_store.list_runs(task.id)
        except Exception:
            runs = []
        latest = runs[-1] if runs else None
        active = bool(getattr(task, "active_run_id", None)) or (
            latest is not None and str(getattr(latest, "status", "")) in {"queued", "running"}
        )
        if active:
            in_flight.append(
                {
                    "task_id": task.id,
                    "name": task.name,
                    "started_at": (
                        latest.started_at.isoformat()
                        if latest is not None and latest.started_at
                        else ""
                    ),
                    "status": str(getattr(latest, "status", "") or "running") if latest else "running",
                }
            )
        elif getattr(task, "next_run_at", None) is not None:
            next_up.append(
                {
                    "task_id": task.id,
                    "name": task.name,
                    "next_run_at": task.next_run_at.isoformat(),
                }
            )

    next_up.sort(key=lambda item: item["next_run_at"])
    return {"in_flight": in_flight, "next_up": next_up[:3]}


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

    try:
        if scheduler_store is not None:
            for run, task in scheduler_store.recent_runs(limit=ACTIVITY_LIMIT):
                # Prefer when it finished; a running one has no finish, so its start
                # is the most recent thing true about it.
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

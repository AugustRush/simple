"""Where a finished run speaks.

Why this exists
---------------
The agent works while nobody is watching. Until now the *only* place that work
showed up was a status panel: a run finished, a card appeared, and the person
found out by going to look. That is a dashboard's relationship, not an agent's --
an assistant that does something and says nothing is a cron job with a nicer UI.

So a finished run writes one line into the conversation it belongs to. Not the
whole artifact (that is delivered and stored, and re-reading it in the transcript
would bury every other message), and not every heartbeat (a status is what it is
doing now; a report is what it did).

Why a bus and not a queue
-------------------------
The durable half (the journal row) and the live half (the push to whichever page
has that conversation open) have to be the *same* sentence, or a reload shows
something the reader never saw arrive. One publish, two consumers, in this
process: the scheduler publishes, and whoever can record and push subscribes.

The bus is deliberately in-memory and synchronous. A run's report is not worth an
outbox: if the process dies between the run finishing and the report landing, the
run is still in the run history with its status, which is where a person looks for
a missing message anyway.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional

#: Journal event type. Named for the screen, not the producer: `run_report` is a
#: run's report whichever executor wrote it.
EVENT_TYPE = "run_report"

#: Statuses worth a sentence in the conversation. `skipped` is not a thing that
#: happened, and a run that is still going has nothing to report yet.
REPORTABLE_STATUSES = frozenset({"succeeded", "failed", "interrupted", "cancelled"})


_Listener = Callable[[dict[str, Any]], None]
_listeners: list[_Listener] = []


def subscribe(listener: _Listener) -> Callable[[], None]:
    """Register a live listener; returns the call that removes it.

    Duplicate registration is allowed on purpose: two channels (a web page and,
    later, a phone) both want the sentence, and one of them unsubscribing must
    not silence the other.
    """
    _listeners.append(listener)

    def unsubscribe() -> None:
        try:
            _listeners.remove(listener)
        except ValueError:
            pass

    return unsubscribe


def build_report(
    *,
    task_name: str,
    task_id: str,
    run_id: str,
    status: str,
    summary: str = "",
    error: str = "",
    text: str = "",
    kind: str = "",
) -> Optional[dict[str, Any]]:
    """The one sentence a run gets, or ``None`` when it does not deserve one.

    Three silences, and each has a reason:

    * **a status that is not a result** (``queued``/``skipped``/``running``) has
      not finished happening;
    * **an internal errand** -- the agent's own housekeeping (memory tidy) -- is
      machinery. It ran because the system maintains itself, not because anybody
      asked for a result, and a nightly "memory tidied" is the message that
      teaches a person to close the conversation;
    * **a result with nothing in it** -- no summary, no output -- is a run that
      did nothing worth saying, and inventing a line for it is noise dressed as
      transparency.

    Everything else speaks, failures most of all: silence after a failure is
    indistinguishable from the agent having forgotten the task, which is the one
    thing an autonomous worker must never look like.
    """
    clean_status = str(status or "").strip()
    if clean_status not in REPORTABLE_STATUSES:
        return None
    if str(kind or "").strip() == "system_job":
        return None
    body = _one_line(summary)
    detail = _one_line(text)
    problem = _one_line(error)
    if clean_status != "succeeded":
        # A failure always speaks: if nothing said what went wrong, the report
        # says that much rather than staying quiet.
        body = body or problem or "没有说明原因"
    if not body and not detail:
        return None
    return {
        "kind": "run_report",
        "task_id": str(task_id or ""),
        "run_id": str(run_id or ""),
        "task_name": str(task_name or "").strip() or "一次定时任务",
        "status": clean_status,
        "text": body or detail,
        # Kept apart from the text so the row can show a failure's reason without
        # pasting it twice when the summary already carries it.
        "error": problem if problem and problem != body else "",
        "at": datetime.now(timezone.utc).isoformat(),
    }


def publish(report: dict[str, Any]) -> None:
    """Hand one report to every listener, and never to a caller's exception.

    A listener is a channel: one that is down must not fail the run that already
    succeeded, and must not stop the other listeners from hearing about it.
    """
    for listener in tuple(_listeners):
        try:
            listener(report)
        except Exception:  # pragma: no cover - a channel's failure is its own
            continue


def _one_line(text: Any, limit: int = 600) -> str:
    """Collapse whitespace and cap length: a transcript row, not a document.

    Newlines are collapsed rather than kept: the row is one sentence in a
    conversation, and the artifact the run stored keeps the formatting.
    """
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"

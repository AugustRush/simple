"""A turn that stopped mid-task, named back to the model at the moment it can act.

The gap this closes
-------------------
Setting a goal used to depend entirely on a line in the static prompt: the model
was told the ``set_goal`` tool exists and when it is appropriate, and then it was
on its own to notice that *this* turn was one of those moments. Measured over a
week of real use: it never once did. The prompt describes a habit; nothing tied
it to the specific situation the habit is for.

What is deterministic here, and what is not
-------------------------------------------
The **trigger** is: the session's most recent ``turn_finished`` event carries a
cancellation, no open goal already covers it, and the person has not dismissed the
"继续任务" card for it. Those are facts this code can read, not judgements.

The **decision** stays with the model. It may judge that the unfinished thing
should be dropped, or that it was not really a task, or that it is about to be
finished in this very turn. What it must not do is fail to notice.

Why not set the goal automatically
----------------------------------
That would be a lie the moment it appeared on the board. The panel says
"它给自己定的目标" -- a goal *it* decided. A row the system wrote on its
interruption would make that sentence false, and the board is the one place the
person goes to find out what the agent is actually up to. The system's job is to
make sure the opportunity is not missed; the intention has to be the model's.

The wording was measured, not guessed
-------------------------------------
The first version said only "if this should continue, record it". Measured on a
real cancelled turn: the model answered the person's *new* question with "我来做。
先建目录和 30 个文件" -- it dropped what the person had just asked and went back
to the old task. So the block now opens by saying to answer the current message
first, and forbids resuming the old work in this turn. A reminder that can hijack
the conversation is worse than no reminder.

**Default-on, not opt-in.** With "if this should continue", measured over three
consecutive cancellations, the model recorded nothing -- every time it judged the
interrupted task not worth resuming and never said so. A cancelled turn is the
strongest signal of unfinished work this system has, so the block now says to
record it *by default*, with an explicit exit: if it decides the task should not
continue, it has to say why in its reply. That turns an invisible skip into a
sentence the person can disagree with.

When it stops
-------------
Three exits, all real. The turn that answers the nudge writes its own
``turn_finished`` with an empty error, so the next call sees a finished turn --
that is the one-shot guarantee, and it needs no counter. Before that, an open goal
covering the task silences it (the model already acted), and so does a dismissal
from the person: dismissing the "继续任务" card writes ``dismissed`` into the
working state, which is an explicit "not this one". A nudge that outlived any of
the three would be the background noise that teaches a reader to skip the block.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = ["resume_nudge", "UNFINISHED_STATUSES"]

#: The statuses the "继续任务" card is built from -- a turn that stopped without
#: finishing what it was doing. `completed`/`dismissed` are deliberately absent:
#: one finished, the other was answered by a person.
UNFINISHED_STATUSES = frozenset({"cancelled", "interrupted", "failed"})


def resume_nudge(
    *,
    store: Optional[Any] = None,
    goal_store: Optional[Any] = None,
    session_id: str = "",
) -> str:
    """The nudge block, or ``""`` when the previous turn finished.

    Reads the last ``turn_finished`` event rather than the working state, and
    that is the whole design. The working state is a *projection of the current
    turn*: measured, a user message is projected into it as ``in_progress``
    before the turn is prepared, so by the time this runs the interrupted status
    from the previous turn has already been overwritten. The first version read
    it and could never fire -- confirmed in a browser with a seeded cancelled
    state, where the debug line reported ``top_status=in_progress``.

    Events are append-only and nothing rewrites them. One row carries both facts
    this needs: ``error`` (did that turn end badly) and ``user_content`` (what
    the task was).

    One-shot by construction: the turn that answers this nudge writes its own
    ``turn_finished`` with an empty error, so the next call sees a finished turn
    and says nothing. No counter, no dismiss flag, nothing to forget to clear.
    """
    clean = str(session_id or "").strip()
    if not clean:
        return ""
    store_ = _store(store)
    event = _last_finished_turn(store_, clean)
    if not event:
        return ""
    payload = getattr(event, "payload", None)
    if not isinstance(payload, dict):
        return ""
    error = str(payload.get("error") or "").strip()
    if not error:
        return ""
    # Only a stop, not a transient failure. A provider 429 is an error the person
    # saw and can retry; it is not work left half-done, and treating it as one
    # would turn every rate limit into a self-set goal.
    lowered = error.lower()
    if "cancel" not in lowered and "interrupt" not in lowered:
        return ""
    goal = str(payload.get("user_content") or "").strip()
    if not goal:
        return ""
    if _already_a_goal(goal_store, goal, since=str(getattr(event, "created_at", "") or "")):
        return ""
    if _dismissed(store_, clean, goal):
        return ""
    return (
        "## 上一轮没有做完\n"
        f"上一次的任务是「{goal[:200]}」（上次停在：{error}），它停在了中途。\n"
        "**先回答我当前这条消息。** 然后**默认**该用 `set_goal` 把它记下来——写清楚目标"
        "和你为什么认为它值得继续，之后的巡逻会提醒你接着做。只有当你判断这件事确实不该"
        "继续（做完了、我改主意了、或者它本来就不是一件事）时才不用记，并在回复里说一句。"
        "不要因为看到这段就丢下我现在说的话去做那件事；也不要现在就去做它。"
    )


def _store(store: Optional[Any]) -> Any:
    """The memory store, opened the same way ``outstanding_block`` opens its own.

    Not ``context_manager.store``: that is an implementation detail the
    ``ContextService`` protocol deliberately does not declare, and
    ``tests/test_memory_contract`` refuses a reader that reaches through it.
    Measured -- the first version did exactly that and the guard named it.
    """
    if store is not None:
        return store
    try:
        from agent import shared
        from agent.memory.store import LTMStore

        return LTMStore(shared.AGENT_HOME / "context")
    except Exception:
        return None


def _last_finished_turn(store: Optional[Any], session_id: str) -> Optional[Any]:
    """The most recent ``turn_finished`` event, or ``None``.

    Scanned backwards over a small window rather than queried by type: the
    journal's reader returns recent events and the terminal one is within a few
    rows of the end even when tools were busy.
    """
    if store is None:
        return None
    read = getattr(store, "recent_agent_events", None)
    if not callable(read):
        return None
    try:
        events = list(read(session_id=session_id, limit=40) or ())
    except Exception:
        return None
    for event in reversed(events):
        if str(getattr(event, "event_type", "") or "") == "turn_finished":
            return event
    return None


def _dismissed(store: Optional[Any], session_id: str, goal: str) -> bool:
    """Whether the person answered the 继续任务 card with "don't continue".

    The card's dismiss writes only to ``session_working_state``, and this block
    deliberately does not read that state for its *trigger* (it is a projection of
    the current turn, so the previous turn's status is already gone by the time
    this runs). But a dismissal is an explicit answer from the person, and a nudge
    that ignored it would set a goal for the exact task they just waved away --
    which is the one thing worse than never reminding them at all.

    Matched on the task text rather than on time: the dismiss writes the *task
    entry's* own ``updated_at``, not the moment of dismissal, so a timestamp
    comparison has nothing to compare against.
    """
    if store is None:
        return False
    load = getattr(store, "load_session_working_state", None)
    if not callable(load):
        return False
    try:
        snapshot = load(session_id)
    except Exception:
        return False
    state = getattr(snapshot, "state", None)
    if not isinstance(state, dict):
        return False
    entries: list[Any] = list(state.get("tasks") or [])
    if str(state.get("active_goal") or "").strip():
        entries.append(state)
    wanted = _normalized(goal)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("status") or "").strip().lower() != "dismissed":
            continue
        text = _normalized(str(entry.get("active_goal") or ""))
        if text and (text in wanted or wanted in text):
            return True
    return False


def _already_a_goal(goal_store: Optional[Any], goal: str, since: str = "") -> bool:
    """Whether the model already answered this interruption.

    Two ways, in order of reliability:

    **By time.** A goal created at or after the moment the working state was last
    updated means the model already acted on this interruption. This is exact and
    is the one that matters, because it is the case that repeats: once the goal
    is recorded, the nudge must stop or it becomes background noise.

    **By text, as containment only.** A goal set *before* the interruption that
    already says the same thing (the task was restated rather than re-decided).

    There was a fuzzy matcher here first and it was removed after measuring it:
    character-bigram overlap scored the *same* task reworded at 0.250
    ("写一份周报" / "每周五写周报") and two *different* tasks at 0.500
    ("修复登录 bug" / "修复注册 bug"). A metric that ranks those backwards cannot
    be tuned into correctness, so the exact signal carries the job and the text
    comparison stays at plain containment.
    """
    if goal_store is None:
        try:
            from agent.autonomy.store import open_proposal_store

            goal_store = open_proposal_store()
        except Exception:
            return False
    try:
        rows = goal_store.list_goals()
    except Exception:
        return False
    horizon = _parse_moment(since)
    wanted = _normalized(goal)
    for row in rows or ():
        entry = row if isinstance(row, dict) else {}
        if horizon is not None:
            created = _parse_moment(str(entry.get("created_at") or ""))
            if created is not None and created >= horizon:
                return True
        text = _normalized(str(entry.get("goal") or ""))
        if text and (text in wanted or wanted in text):
            return True
    return False


def _normalized(text: str) -> str:
    return " ".join(str(text or "").split()).lower()


def _parse_moment(value: str) -> Optional[Any]:
    """Read either timestamp spelling this codebase uses, or give up quietly.

    ``session_working_state`` stores ``2026-11-18 12:31:06.436453 UTC``; the goals
    table stores ISO-8601. They are the same instant written by two layers, and a
    comparison between them has to read both.
    """
    from datetime import datetime, timezone

    clean = str(value or "").strip()
    if not clean:
        return None
    if clean.endswith(" UTC"):
        clean = clean[: -len(" UTC")] + "+00:00"
    if " " in clean and "T" not in clean:
        clean = clean.replace(" ", "T", 1)
    try:
        moment = datetime.fromisoformat(clean)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
